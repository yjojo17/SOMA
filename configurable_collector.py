"""
Configurable Multi-Account Instagram Feed Collector
With TimescaleDB persistence and raw-response archive.
"""
import sys
import time
import random
import logging
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import yaml
import subprocess

from pyvirtualdisplay import Display
#from selenium import webdriver
from undetected_geckodriver import Firefox
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains

from extension_interceptor import ExtensionInterceptor
from capture_addon import install_capture_extension
from human_behavior import HumanBehavior
from action_logger import ActionLogger
from db.database_manager import DatabaseManager
from db.raw_archive import RawArchive
from monitoring.metrics_pusher import MetricsPusher
from clip_classifier import CLIPClassificationService


class ConfigurableNetworkCollector:

    def __init__(self, profile_id: str, config_file: str = 'research_config.yaml', use_virtual_display: bool = False):
        self.profile_id = profile_id
        self.config_file = config_file
        self.use_virtual_display = use_virtual_display

        self.config = self._load_config()
        self.profile_config = self._get_profile_config(profile_id)

        self.do_interact = self.profile_config.get('condition') == 'interaction'

        self.logger = self._setup_logging()

        self.display = None
        self.driver = None
        self.human_behavior = HumanBehavior(self.logger)
        self.action_logger = None

        # Per-session state, set in collect_feed
        self.session_id: Optional[str] = None
        self.db: Optional[DatabaseManager] = None
        self.archive: Optional[RawArchive] = None
        self.interceptor: Optional[ExtensionInterceptor] = None
        self.pusher: Optional[MetricsPusher] = None

        self.current_scroll_position = 0
        self.current_post_data = None
        self.followed_accounts = set()
        self.attempted_like_posts = set()
        self.attempted_follow_posts = set()
        self.processed_posts = set()
        # CLIP-aligned watch time: off during WarmUp, set true for the interaction
        # phase so dwell length tracks whether a post is on-target.
        self.clip_aligned_watch = self.config['collection_settings'].get('clip_aligned_watch', False)
        self.save_feed_data = self.config['collection_settings'].get('save_feed_data', False)
        # Minimum calibrated probability for the target bucket before a post counts
        # as on-target (gates likes and clip-aligned watch). Softmax runs over the
        # full category set
        p = self.config.get('interaction_policy', {})
        self.policy        = p
        self.tau_like      = p.get('tau_like',   1.01)   # 1.01 = unreachable: no likes if unset
        self.tau_dwell     = p.get('tau_dwell',  1.01)
        self.tau_follow    = p.get('tau_follow', 1.01)
        self.like_budget     = 0   # set per session in collect_feed()
        self.follow_budget   = 0
        self.follow_day_left = 0

        # CLIP service: looks up bucket definitions by the profile's bucket key.
        seen = {}
        for entries in self.config.get('bucket_definitions', {}).values():
            for b in entries:
                seen.setdefault(b['name'], b)
        all_categories = list(seen.values())
        self.vlm_service = CLIPClassificationService(all_categories) if all_categories else None  # full set — unchanged

        bucket_cats = self.config.get('bucket_definitions', {}).get(self.profile_config.get('bucket', ''), [])
        self._target_names = [b['name'] for b in bucket_cats if not b.get('neutral', False)]


    def _load_config(self) -> Dict:
        with open(self.config_file, 'r') as f:
            return yaml.safe_load(f)

    def _get_profile_config(self, profile_id: str) -> Dict:
        for profile in self.config['research_profiles']:
            if profile['id'] == profile_id:
                return profile
        raise ValueError(f"Profile {profile_id} not found in configuration")

    def _setup_logging(self) -> logging.Logger:
        log_dir = Path('logs')
        log_dir.mkdir(exist_ok=True)

        logger = logging.getLogger(f'collector_{self.profile_id}')
        logger.setLevel(logging.INFO)

        fh = logging.FileHandler(log_dir / f'{self.profile_id}.log')
        fh.setLevel(logging.INFO)

        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        return logger

    def get_current_post_in_view(self):
        """Identifies which post is currently most visible in the viewport."""
        articles = self.driver.find_elements(By.TAG_NAME, 'article')
        viewport_height = self.driver.execute_script("return window.innerHeight;")

        for index, article in enumerate(articles):
            position = self.driver.execute_script(
                "return arguments[0].getBoundingClientRect();", article
            )
            if 0 <= position['top'] <= (viewport_height * 0.5):
                network_posts = self.interceptor.get_posts()
                if index < len(network_posts):
                    return network_posts[index]
        return None

    def _get_post_link_from_article(self, article) -> Optional[str]:
        try:
            link_elem = article.find_element(By.XPATH, ".//a[contains(@href, '/p/') or contains(@href, '/reel/')]")
            href = link_elem.get_attribute('href').split('?')[0]
            parts = [p for p in href.split('/') if p]
            if parts[-1] in ('liked_by', 'comments', 'share'):
                return None
            return href
        except Exception:
            return None

    def _shortcode(self, url: str) -> str:
        parts = [p for p in url.split('?')[0].split('/') if p]
        for marker in ('p', 'reel'):
            if marker in parts and parts.index(marker) + 1 < len(parts):
                return parts[parts.index(marker) + 1]
        return url

    def update_context(self):
        """Returns the centered article element, or None if no match.

        Also submits any newly captured posts to CLIP so inference starts
        as early as possible — results checked later in _vlm_fits().
        """
        self.interceptor.process_requests(self.driver)
        captured_posts = self.interceptor.get_posts()

        if self.vlm_service:
            for post_data in captured_posts:
                self.vlm_service.submit(post_data)

        articles = self.driver.find_elements(By.TAG_NAME, 'article')
        viewport_h = self.driver.execute_script("return window.innerHeight;")
        center = viewport_h / 2

        for i, article in enumerate(articles):
            try:
                rect = self.driver.execute_script("return arguments[0].getBoundingClientRect();", article)
            except StaleElementReferenceException:
                continue
            if rect['top'] < center < rect['bottom']:
                link = self._get_post_link_from_article(article)
                if link:
                    link_code = self._shortcode(link)
                    for post_data in captured_posts:
                        if self._shortcode(post_data.get('post_link', '')) == link_code:
                            self.action_logger.set_active_post(post_data)
                            self.current_post_data = post_data
                            return article
                    self.logger.info("  -> article not yet in network data, returning element anyway")
                    self.action_logger.set_active_post(None)
                    self.current_post_data = None
                    return article
                self.action_logger.set_active_post(None)
                self.current_post_data = None
                return None

        self.action_logger.set_active_post(None)
        self.current_post_data = None
        return None

    def _activation(self, post_link: str) -> float:
        """Max CLIP probability across the account's high-interest categories.
        Synchronous: classifies the centered post now (cached), so the live like/
        follow decision never races the async queue."""
        if not self.vlm_service or not self.current_post_data or not self._target_names:
            return 0.0
        result = self.vlm_service.classify_sync(self.current_post_data)
        if not result:
            return 0.0
        a = result.best_target_score(self._target_names)
        self.logger.info(f"CLIP {post_link.split('/')[-2]} — top: {result.top_bucket} a={a:.3f}")
        return a

    def _engagement_tier(self, post_link: str) -> str:
        """'high' -> like-eligible, 'medium' -> long dwell only, 'baseline' -> normal."""
        a = self._activation(post_link)
        if a >= self.tau_like:
            return 'high'
        if a >= self.tau_dwell:
            return 'medium'
        return 'baseline'

    def _carousel_depth(self, post_link: str) -> int:
        """Slides to click through, from CLIP. Cover is already classified; refine
        with slide 2 / last when their URLs were captured (carousel_image_urls)."""
        a = self._activation(post_link)
        extra = (self.current_post_data or {}).get('carousel_image_urls', [])[1:]
        if extra and self.vlm_service and self._target_names:
            caption = (self.current_post_data or {}).get('caption', '') or ''
            a = max([a] + [self.vlm_service.activation(u, self._target_names, caption) for u in extra])
        if a >= self.tau_like:
            return 99                                            # all slides
        if a >= self.tau_dwell:
            return self.policy.get('carousel_depth_medium', 3)   # a few
        return 1                                                 # cover only

    def _maybe_like(self, article, post_link: str, tier: str) -> None:
        """The single like path: budget + attempted + per-tier probability gate.
        MEDIUM never likes (dwell-only); HIGH and BASELINE fire at their own rates."""
        if not self.do_interact or self.like_budget <= 0 or post_link in self.attempted_like_posts:
            return
        if tier == 'high':
            p_fire = self.policy.get('high_band_like_prob', 0.0)
        elif tier == 'baseline':
            p_fire = self.policy.get('off_bucket_like_prob', 0.0)
        else:
            return
        if random.random() >= p_fire:
            return
        self.attempted_like_posts.add(post_link)
        if self.perform_like_action(article):
            self.like_budget -= 1
            time.sleep(random.uniform(1.5, 3.0))

    def _watch_target(self, post_link: str) -> float:
        """Video watch fraction for this post. Passes the clip result only when
        clip_aligned_watch is on, so WarmUp stays content-agnostic."""
        fits = (self._engagement_tier(post_link) in ('high', 'medium')) if self.clip_aligned_watch else None
        return self.human_behavior.video_watch_fraction(fits)

    def _is_carousel_article(self, article) -> bool:
        """Returns True if the article is a multi-slide carousel.

        Must be checked before _is_video_article — Instagram pre-loads <video> elements
        for all carousel slides in the DOM simultaneously, including non-visible ones.
        A carousel with a video on slide 2 would otherwise be misidentified as a video post,
        causing _wait_for_video_progress to poll a hidden, non-playing video and hit the
        full 60s timeout.
        """
        try:
            return self.driver.execute_script("""
                return arguments[0].querySelector('[aria-label="Next"]') !== null
                    || arguments[0].querySelector('[aria-label="Nächstes"]') !== null
                    || arguments[0].querySelector('[aria-label="Weiter"]') !== null;
            """, article)
        except StaleElementReferenceException:
            return False

    def _handle_carousel(self, article, post_link: str, max_slides: int = 99):
        """Clicks through carousel slides, dwelling on each. Stops gracefully if the
        article goes stale (IG re-renders the feed during long video waits)."""
        slide_num = 0
        try:
            while True:
                is_video = self._is_video_article(article)
                self.logger.info(f"Carousel slide {slide_num}: {'video' if is_video else 'image'}")
                if is_video:
                    self._wait_for_video_progress(article, self._watch_target(post_link))
                else:
                    time.sleep(random.uniform(1.5, 3.5))

                if slide_num + 1 >= max_slides:
                    break

                next_btn = self.driver.execute_script("""
                    return arguments[0].querySelector('[aria-label="Next"]')
                        || arguments[0].querySelector('[aria-label="Nächstes"]')
                        || arguments[0].querySelector('[aria-label="Weiter"]');
                """, article)
                if next_btn is None:
                    break

                self.driver.execute_script("arguments[0].click();", next_btn)
                time.sleep(random.uniform(2.0, 3.5))
                self.driver.execute_script("""
                    var videos = arguments[0].querySelectorAll('video');
                    for (var v of videos) {
                        if (v.offsetWidth > 0 && v.offsetHeight > 0 && v.paused) {
                            v.play();
                            break;
                        }
                    }
                """, article)
                slide_num += 1
        except StaleElementReferenceException:
            self.logger.warning(f"Carousel went stale at slide {slide_num} — moving on")

        self.action_logger.log_action('carousel_viewed', {'slides_viewed': slide_num + 1})

    def _is_video_article(self, article) -> bool:
        """Returns True if the article's currently visible slide contains a playing video.

        Uses offsetWidth/offsetHeight rather than querySelector to exclude preloaded
        but hidden <video> elements that Instagram places in the DOM for all carousel
        slides simultaneously.
        """
        try:
            return self.driver.execute_script("""
                var videos = arguments[0].querySelectorAll('video');
                for (var v of videos) {
                    if (v.offsetWidth > 0 && v.offsetHeight > 0) return true;
                }
                return false;
            """, article)
        except StaleElementReferenceException:
            return False

    def _wait_for_video_progress(self, article, target_pct: float, timeout: float = 60.0):
        """Blocks until the visible video reaches target_pct, or timeout. Returns
        quietly if the element goes stale mid-watch."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                result = self.driver.execute_script("""
                    var videos = arguments[0].querySelectorAll('video');
                    for (var v of videos) {
                        if (v.offsetWidth > 0 && v.offsetHeight > 0 && v.duration) {
                            return {currentTime: v.currentTime, duration: v.duration};
                        }
                    }
                    return null;
                """, article)
            except StaleElementReferenceException:
                self.logger.warning("Video element went stale mid-watch — moving on")
                return
            if result and result['duration'] > 0:
                progress = result['currentTime'] / result['duration']
                if progress >= target_pct:
                    self.action_logger.log_action('video_watched', {
                        'progress_pct': round(progress * 100, 1),
                        'target_pct': round(target_pct * 100, 1),
                        'duration_s': round(result['duration'], 1),
                    })
                    return
            time.sleep(0.5)
        self.logger.debug("Video watch timeout reached before target percentage")

    def perform_like_action(self, article):
        try:
            rect = self.driver.execute_script("return arguments[0].getBoundingClientRect();", article)
            self.logger.info(f"perform_like_action: article rect top={rect['top']:.0f}, bottom={rect['bottom']:.0f}, height={rect['height']:.0f}")
            time.sleep(self.human_behavior.pre_like_pause())

            all_labeled = article.find_elements(By.XPATH, ".//*[@aria-label]")
            like_el = None
            for el in all_labeled:
                label = el.get_attribute('aria-label')
                self.logger.info(f"  tag={el.tag_name}, aria-label='{label}'")
                if label == 'Like' or label == 'Gefällt mir':
                    like_el = el

            photo_elements = article.find_elements(By.XPATH, ".//img[@style]")

            if self.human_behavior.double_tap_likelihood() and photo_elements:
                self.logger.info("Like attempt: double_tap")
                ActionChains(self.driver).double_click(photo_elements[0]).perform()
                self.logger.info("Like success: double_tap")
            elif like_el is not None:
                self.logger.info("Like attempt: heart_button")
                self.driver.execute_script("arguments[0].parentElement.click();", like_el)
                self.logger.info("Like success: heart_button")
            else:
                self.logger.warning("Like skipped: no Like element found in article")
                return False

            self.action_logger.log_like()
            time.sleep(self.human_behavior.post_like_pause())
            return True

        except StaleElementReferenceException:
            self.logger.warning("Like skipped: stale article reference")
            return False
        except Exception as e:
            self.logger.warning(f"Like failed: {e}")
            return False

    def _is_suggested_in_dom(self, article) -> bool:
        """Detects suggested post indicator from the DOM when network data isn't available yet."""
        try:
            article.find_element(
                By.XPATH, ".//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'suggested for you') or contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'vorgeschlagen')]"
            )
            return True
        except Exception:
            return False

    def _get_username_from_dom(self, article) -> str:
        """Extracts username from article header link as fallback when network data isn't available."""
        try:
            link = article.find_element(By.XPATH, ".//header//a[@href]")
            href = link.get_attribute('href')
            return href.rstrip('/').split('/')[-1]
        except Exception:
            return ''

    def perform_follow_action(self, article, username: str) -> bool:
        try:
            self.logger.info("In following function")

            role_buttons = article.find_elements(By.XPATH, ".//*[@role='button']")
            for el in role_buttons:
                self.logger.info(f"  role=button tag={el.tag_name} text='{el.text.strip()[:50]}' aria-label='{el.get_attribute('aria-label')}'")
            follow_btn = article.find_element(
                By.XPATH,
                ".//*[@role='button' and (normalize-space()='Follow' or normalize-space()='Folgen'"
                " or normalize-space(.)='Follow' or normalize-space(.)='Folgen')]"
            )
            time.sleep(self.human_behavior.pre_like_pause())
            follow_btn.click()
            self.logger.info(f"Follow button clicked for {username}")
            self.followed_accounts.add(username)

            for post in self.interceptor.get_posts():
                if post.get('profile_name') == username:
                    post['content_type']['follow_action_taken'] = True
                    post['content_type']['follow_timestamp'] = datetime.now().isoformat()

            self.action_logger.log_action('follow', {'username': username})
            self.logger.info(f"Followed suggested account: {username}")
            time.sleep(self.human_behavior.post_like_pause())
            return True

        except StaleElementReferenceException:
            self.logger.warning(f"Follow skipped: stale article reference for {username}")
            return False
        except Exception as e:
            self.logger.warning(f"Follow failed for {username}: {e}")
            return False

    def _clear_profile_lock(self, profile_path: str) -> None:
        """Remove stale Firefox profile locks left by a crashed prior session — a held
        lock is what triggers SessionNotCreatedException: 'Failed to set preferences'."""
        for name in ('.parentlock', 'parent.lock', 'lock'):
            lock = Path(profile_path) / name
            try:
                if lock.is_symlink() or lock.exists():
                    lock.unlink()
                    self.logger.info(f"Removed stale profile lock: {lock}")
            except OSError as e:
                self.logger.warning(f"Could not remove {lock}: {e}")

    def _kill_stray_browser(self, profile_path: str) -> None:
        """undetected_geckodriver's failed-init cleanup is broken — its quit() raises
        before killing the Firefox it spawned, so a failed launch orphans a Firefox
        still bound to this profile. A live holder (or a manual window left open) makes
        every later launch fail with 'Failed to set preferences'. Kill it by profile path."""
        try:
            subprocess.run(['pkill', '-f', profile_path], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            self.logger.warning("pkill unavailable — cannot clear stray browser processes")
            return
        time.sleep(1.0)   # let the OS reap before relaunch

    def initialize_browser(self, max_attempts: int = 3):
        screen_w, screen_h = self.profile_config['screen']    # virtual monitor (Xvfb)
        window_w, window_h = self.profile_config['window']    # browser window
        firefox_profile = self.profile_config['firefox_profile']

        if self.use_virtual_display:
            self.display = Display(visible=0, size=(screen_w, screen_h))
            self.display.start()
            self.logger.info(f"Virtual display started for {self.profile_id}")

        self._kill_stray_browser(firefox_profile)       

        for attempt in range(1, max_attempts + 1):
            self._clear_profile_lock(firefox_profile)

            options = Options()
            options.add_argument('-profile')
            options.add_argument(firefox_profile)
            try:
                self.driver = Firefox(
                    service=Service('/usr/local/bin/geckodriver'),
                    options=options,
                )
                break
            except Exception as e:
                # undetected_geckodriver masks the real SessionNotCreatedException
                # ("Failed to set preferences") with a TypeError from its own failed
                # quit(); both mean the profile wouldn't open and both clear on retry.
                self.logger.warning(
                    f"Browser init attempt {attempt}/{max_attempts} for {self.profile_id} "
                    f"failed (likely 'Failed to set preferences' — stale lock / transient): {e}"
                )
                self._kill_stray_browser(firefox_profile)       
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"Browser failed to start for {self.profile_id} after {max_attempts} attempts"
                    ) from e
                time.sleep(random.uniform(3.0, 6.0))

        install_capture_extension(self.driver)
        self.driver.set_window_size(window_w, window_h)
        self.logger.info(f"Browser initialized for {self.profile_id}")

    def collect_feed(self, target_posts: int = 50) -> List[Dict]:
        started_at = datetime.now()
        self.session_id = f"{self.profile_id}_{started_at.strftime('%Y%m%d_%H%M%S')}"

        self.db = DatabaseManager()
        self.db.connect()

        self.pusher = MetricsPusher(account_id=self.profile_id, session_id=self.session_id)
        self.pusher.connect()

        is_probe = self.profile_config.get('role') == 'probe'

        self.db.ensure_account(
            account_id         = self.profile_id,
            email              = self.profile_config['email'],
            firefox_profile    = self.profile_config['firefox_profile'],
            role               = self.profile_config['role'],
            bucket             = self.profile_config.get('bucket') if is_probe else None,
            assigned_interests = None if is_probe else self.profile_config.get('assigned_interests'),
            gender             = None if is_probe else self.profile_config.get('gender'),
            condition          = self.profile_config.get('condition'),
        )
        
        self.action_logger = ActionLogger(self.profile_id, session_id=self.session_id)
        self.archive = RawArchive(self.profile_id, self.session_id) if self.save_feed_data else None
        self.interceptor = ExtensionInterceptor(archive=self.archive)

        session_duration_minutes = self.human_behavior.realistic_session_duration()
        session_end_time = time.time() + (session_duration_minutes * 60)

        self.db.insert_session(
            session_id=self.session_id,
            account_id=self.profile_id,
            started_at=started_at,
            planned_duration_seconds=int(session_duration_minutes * 60),
            target_posts=target_posts,
        )

        # Interaction budgets — read from the interactions table (single source of truth).
        if self.do_interact:
            week_ago  = started_at - timedelta(days=7)
            day_start = started_at.replace(hour=0, minute=0, second=0, microsecond=0)
            likes_wk   = self.db.count_interactions_since(self.profile_id, 'like',   week_ago)
            follows_wk = self.db.count_interactions_since(self.profile_id, 'follow', week_ago)
            follows_td = self.db.count_interactions_since(self.profile_id, 'follow', day_start)

            remaining_week = max(0, self.policy.get('likes_week_cap', 0) - likes_wk)
            if random.random() < self.policy.get('active_session_prob', 0.0) and remaining_week > 0:
                self.like_budget = min(
                    remaining_week,
                    random.randint(1, self.policy.get('session_like_target', 1) + 1),
                    self.policy.get('likes_session_cap', 0),
                )
            else:
                self.like_budget = 0

            self.follow_budget   = max(0, self.policy.get('follow_week_budget', 0) - follows_wk)
            self.follow_day_left = max(0, self.policy.get('follow_day_cap', 0) - follows_td)
            self.logger.info(f"Budgets — likes:{self.like_budget} "
                             f"follows_wk_left:{self.follow_budget} follows_today_left:{self.follow_day_left}")

        self.action_logger.log_session_start({
            'target_posts': target_posts,
            'profile_id': self.profile_id,
            'role': self.profile_config['role'],
            'use_virtual_display': self.use_virtual_display,
            'planned_duration': session_duration_minutes
        })

        self.logger.info(f"Collection started: session={self.session_id} target={target_posts} duration={session_duration_minutes}min")

        try:
            self.driver.get('https://www.instagram.com/')
            WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'article')))

            # Set browser cookies on CLIP session so CDN downloads succeed
            if self.vlm_service:
                self.vlm_service.set_cookies(self.driver.get_cookies())
                self.logger.info("CLIP cookies set")

            scroll_count = 0
            max_scrolls = 200

            while scroll_count < max_scrolls and time.time() < session_end_time:
                # 1. What is centered right now — before any scrolling
                article = self.update_context()
                post_link = (
                    (self.current_post_data.get('post_link') if self.current_post_data else None)
                    or (self._get_post_link_from_article(article) if article else None)
                    or ''
                )

                dwell_start = time.time()
                is_new_post = article is not None and post_link and post_link not in self.processed_posts

                # 2. Tier from CLIP (interaction arm only; passive accounts never block on CLIP here)
                tier = self._engagement_tier(post_link) if (self.do_interact and is_new_post) else 'baseline'

                # 2a. Dwell on whatever is centered — depth/duration set by tier
                if article is None or post_link in self.processed_posts:
                    time.sleep(random.uniform(0.3, 0.7))
                elif self._is_carousel_article(article):
                    if self.do_interact:
                        self._handle_carousel(article, post_link, self._carousel_depth(post_link))
                    else:
                        time.sleep(random.uniform(1.5, 3.5))
                    self.processed_posts.add(post_link)
                elif self._is_video_article(article):
                    if self.do_interact:
                        self._wait_for_video_progress(article, self._watch_target(post_link))
                    else:
                        time.sleep(random.uniform(2.0, 4.0))
                    self.processed_posts.add(post_link)
                else:
                    if tier in ('high', 'medium'):
                        base = self.human_behavior.content_dwell_time('image')
                        dwell = self.human_behavior.long_dwell(base, *self.policy.get('dwell_multiplier', [2.0, 4.0]))
                        self.action_logger.log_pause(duration=dwell)
                        time.sleep(dwell)
                    elif self.human_behavior.should_pause():
                        pause_duration = self.human_behavior.pause_duration()
                        self.action_logger.log_pause(duration=pause_duration)
                        time.sleep(pause_duration)
                    self.processed_posts.add(post_link)

                # 2b. Like decision — one routed path (budget + tier handled inside _maybe_like)
                if is_new_post:
                    self._maybe_like(article, post_link, tier)

                # 3. Follow suggested accounts — CLIP-fit (tau_follow) + weekly/daily budget
                if (article is not None and self.profile_config.get('follow_suggested', False)
                        and self.follow_budget > 0 and self.follow_day_left > 0
                        and self._activation(post_link) >= self.tau_follow):
                    is_suggested = (
                        (self.current_post_data is not None and self.current_post_data.get('is_suggested'))
                        or self._is_suggested_in_dom(article)
                    )
                    if is_suggested and post_link not in self.attempted_follow_posts:
                        self.attempted_follow_posts.add(post_link)
                        username = (
                            self.current_post_data.get('profile_name', '') if self.current_post_data
                            else self._get_username_from_dom(article)
                        )
                        if username and username not in self.followed_accounts:
                            if self.perform_follow_action(article, username):
                                self.follow_budget   -= 1
                                self.follow_day_left -= 1

                # Log total dwell time for newly processed posts
                if is_new_post and post_link in self.processed_posts:
                    self.action_logger.log_action('post_view', {
                        'duration_s': round(time.time() - dwell_start, 2)
                    })

                # 4. Check target before scrolling further
                if len(self.interceptor.get_posts()) >= target_posts:
                    break

                # 5. Occasional mouse movement
                mouse_data = self.human_behavior.mouse_movement(self.driver)
                if mouse_data:
                    self.action_logger.log_mouse_move(mouse_data)

                # 6. Scroll to next post
                scroll_data = self.human_behavior.variable_scroll(self.driver)
                self.current_scroll_position = self.driver.execute_script("return window.pageYOffset;")
                self.action_logger.log_scroll(scroll_data, self.current_scroll_position)

                if self.human_behavior.should_back_scroll():
                    back_distance = self.human_behavior.back_scroll_distance(self.driver)
                    self.driver.execute_script(f"window.scrollBy(0, {back_distance});")
                    self.action_logger.log_action('back_scroll', {'distance_px': back_distance})
                    time.sleep(random.uniform(0.5, 1.5))

                time.sleep(self.human_behavior.scroll_delay())
                scroll_count += 1

                if scroll_count % 10 == 0:
                    self.pusher.push()

            # ── Post-session: capture, classify, enrich ───────────────────────
            self.interceptor.process_requests(self.driver)
            network_posts = self.interceptor.get_posts()

            self.action_logger.log_api_intercept(
                endpoint='feed/timeline',
                posts_count=len(network_posts)
            )

            # Submit any posts not yet queued (captured in the final process_requests
            # call that runs after the scroll loop — these had no chance to be
            # submitted via update_context during the session).
            if self.vlm_service:
                newly_submitted = 0
                for post in network_posts:
                    if post.get('post_link') not in self.vlm_service._pending:
                        self.vlm_service.submit(post)
                        newly_submitted += 1
                if newly_submitted:
                    self.logger.info(f"CLIP: submitted {newly_submitted} late-captured posts")

            # Enrich posts and collect CLIP results.
            # Since the thread pool is sequential (max_workers=1), by the time
            # result(post_N) returns, post_N+1 has already started — so each
            # subsequent call blocks for only ~one inference cycle, not the
            # full remaining queue.
            for i, post in enumerate(network_posts):
                post['account_id']    = self.profile_id
                post['session_id']    = self.session_id
                post['feed_position'] = i + 1
                if self.vlm_service:
                    clip_result = self.vlm_service.result(post.get('post_link', ''), timeout=15)
                    if clip_result and self._target_names:
                        post['clip_score']   = clip_result.best_target_score(self._target_names)
                        post['clip_aligned'] = clip_result.top_bucket in self._target_names
                    else:
                        post['clip_score']   = None
                        post['clip_aligned'] = None
                    post['clip_top_bucket'] = clip_result.top_bucket if clip_result else None
                    post['vlm_scores']      = clip_result.scores if clip_result else None

            self.logger.info(f"Collection complete: {len(network_posts)} posts")

            final_stats = {
                'posts_collected':  len(network_posts),
                'scrolls_performed': scroll_count,
                'target_reached':   len(network_posts) >= target_posts,
                'suggested_posts':  sum(1 for p in network_posts if p.get('is_suggested', False)),
                'followed_posts':   sum(1 for p in network_posts if p.get('is_following', False)),
                'followed_suggested': len(self.followed_accounts),
            }
            self.action_logger.log_session_end(final_stats)

            self.pusher.record_posts_collected(final_stats['posts_collected'])
            self.pusher.record_posts_suggested(final_stats['suggested_posts'])
            self.pusher.record_posts_followed(final_stats['followed_posts'])
            self.pusher.record_api_intercept('feed/timeline', count=1)

            self._persist_session(network_posts, status='completed', final_stats=final_stats)
            self.action_logger.print_summary()

            return network_posts[:target_posts]

        except Exception as e:
            import traceback
            self.logger.error(f"Collection error: {str(e)}\n{traceback.format_exc()}")
            self.action_logger.log_error('collection_error', str(e))
            if self.pusher:
                self.pusher.record_error('collection_error')
            try:
                partial_posts = self.interceptor.get_posts() if self.interceptor else []
                partial_stats = {'posts_collected': len(partial_posts), 'error': str(e)}
                if self.pusher:
                    self.pusher.record_posts_collected(len(partial_posts))
                self._persist_session(partial_posts, status='errored', final_stats=partial_stats)
            except Exception as persist_err:
                self.logger.error(f"Persist on error also failed: {persist_err}")
            return []

    def _persist_session(self, network_posts: List[Dict], status: str, final_stats: Dict):
        """Close raw archive, write posts + interactions, finalize session row and metrics."""
        archive_path = self.archive.close() if self.archive else None

        pk_to_id = self.db.insert_posts(
            session_id=self.session_id,
            account_id=self.profile_id,
            posts=network_posts,
        )
        self.db.insert_interactions(
            session_id=self.session_id,
            account_id=self.profile_id,
            actions=self.action_logger.actions,
            post_pk_to_id=pk_to_id,
        )
        ended_at = datetime.now()
        self.db.finalize_session(
            session_id=self.session_id,
            ended_at=ended_at,
            status=status,
            final_stats=final_stats,
            raw_archive_path=archive_path,
        )
        if self.pusher:
            duration = (ended_at - self.action_logger.session_start).total_seconds()
            self.pusher.finalize(duration_seconds=duration, status=status)

    def cleanup(self):
        if self.vlm_service:
            self.vlm_service.shutdown()

        if self.driver is not None:
            self.driver.quit()
            self.logger.info(f"Browser closed for {self.profile_id}")

        if self.display:
            self.display.stop()
            self.logger.info(f"Virtual display stopped for {self.profile_id}")

        if self.db:
            self.db.close()
            self.db = None


def main():
    logging.basicConfig(level=logging.INFO)

    test_profiles = ["profile_de_01"]
    target_posts = 50
    delay_between_profiles = (300, 600)

    print("\n" + "="*70)
    print(" MULTI-ACCOUNT COLLECTION TEST")
    print("="*70)
    print(f"Profiles to test: {', '.join(test_profiles)}")
    print(f"Target posts per profile: {target_posts}")
    print(f"Delay between profiles: {delay_between_profiles[0]}-{delay_between_profiles[1]}s")
    print("="*70 + "\n")

    results = {}

    for i, profile_id in enumerate(test_profiles):
        print(f"\n{'='*70}")
        print(f" PROFILE {i+1}/{len(test_profiles)}: {profile_id}")
        print(f"{'='*70}\n")

        collector = None

        try:
            collector = ConfigurableNetworkCollector(
                profile_id=profile_id,
                use_virtual_display=False
            )

            collector.initialize_browser()
            feed_data = collector.collect_feed(target_posts=target_posts)

            if feed_data:
                results[profile_id] = {
                    'success': True,
                    'posts_collected': len(feed_data),
                    'suggested_count': sum(1 for p in feed_data if p.get('is_suggested', False)),
                    'followed_count': sum(1 for p in feed_data if p.get('is_following', False)),
                    'session_id': collector.session_id,
                }
                print(f"\n✓ {profile_id}: {len(feed_data)} posts collected (session {collector.session_id})")
                print(f"  Suggested: {results[profile_id]['suggested_count']}")
                print(f"  Followed: {results[profile_id]['followed_count']}")
            else:
                results[profile_id] = {'success': False, 'error': 'No data collected'}
                print(f"\n✗ {profile_id}: Collection failed")

        except Exception as e:
            results[profile_id] = {'success': False, 'error': str(e)}
            print(f"\n✗ {profile_id}: Error - {e}")

        finally:
            if collector:
                collector.cleanup()

        if i < len(test_profiles) - 1:
            delay = random.uniform(*delay_between_profiles)
            print(f"\n⏳ Waiting {delay/60:.1f} minutes before next profile...")
            time.sleep(delay)

    print("\n" + "="*70)
    print(" COLLECTION SUMMARY")
    print("="*70)
    for profile_id, result in results.items():
        if result['success']:
            print(f"✓ {profile_id}: {result['posts_collected']} posts "
                  f"(Suggested: {result['suggested_count']}, Followed: {result['followed_count']})")
        else:
            print(f"✗ {profile_id}: {result.get('error', 'Unknown error')}")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()