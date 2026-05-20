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
from datetime import datetime
from typing import Optional, Dict, List
import yaml

from pyvirtualdisplay import Display
from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains
from seleniumwire import webdriver as wire_webdriver

from seleniumwire_interceptor import SeleniumWireInterceptor
from human_behavior import HumanBehavior
from action_logger import ActionLogger
from db.database_manager import DatabaseManager
from db.raw_archive import RawArchive
from monitoring.metrics_pusher import MetricsPusher


class ConfigurableNetworkCollector:

    def __init__(self, profile_id: str, config_file: str = 'research_config.yaml', use_virtual_display: bool = False):
        self.profile_id = profile_id
        self.config_file = config_file
        self.use_virtual_display = use_virtual_display

        self.config = self._load_config()
        self.profile_config = self._get_profile_config(profile_id)

        self.passive_mode = self.config['collection_settings'].get('passive_mode', False)
        self.do_interact = (
            self.profile_config.get('condition') == 'interaction'
            and not self.passive_mode
        )

        self.logger = self._setup_logging()

        self.display = None
        self.driver = None
        self.human_behavior = HumanBehavior(self.logger)
        self.action_logger = None

        # Per-session state, set in collect_feed
        self.session_id: Optional[str] = None
        self.db: Optional[DatabaseManager] = None
        self.archive: Optional[RawArchive] = None
        self.interceptor: Optional[SeleniumWireInterceptor] = None
        self.pusher: Optional[MetricsPusher] = None

        self.current_scroll_position = 0
        self.current_post_data = None
        self.followed_accounts = set()
        self.attempted_like_posts = set()
        self.attempted_follow_posts = set()
        self.processed_posts = set()  # posts that have been fully dwelt on
        self.video_watch_pct = self.config['collection_settings'].get('video_watch_percentage', 0.1)
        self.passive_mode = self.config['collection_settings'].get('passive_mode', False)

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
        """Extracts the canonical post URL from a DOM article element."""
        try:
            link_elem = article.find_element(By.XPATH, ".//a[contains(@href, '/p/') or contains(@href, '/reel/')]")
            return link_elem.get_attribute('href').split('?')[0]
        except Exception:
            return None

    def update_context(self):
        """Returns the centered article element, or None if no match."""
        self.interceptor.process_requests(self.driver)
        captured_posts = self.interceptor.get_posts()

        articles = self.driver.find_elements(By.TAG_NAME, 'article')
        viewport_h = self.driver.execute_script("return window.innerHeight;")
        center = viewport_h / 2

        for i, article in enumerate(articles):
            rect = self.driver.execute_script("return arguments[0].getBoundingClientRect();", article)
            if rect['top'] < center < rect['bottom']:
                link = self._get_post_link_from_article(article)
                if link:
                    for post_data in captured_posts:
                        if post_data.get('postLink') == link:
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

    def _is_carousel_article(self, article) -> bool:
        """Returns True if the article is a multi-slide carousel.

        Must be checked before _is_video_article — Instagram pre-loads <video> elements
        for all carousel slides in the DOM simultaneously, including non-visible ones.
        A carousel with a video on slide 2 would otherwise be misidentified as a video post,
        causing _wait_for_video_progress to poll a hidden, non-playing video and hit the
        full 60s timeout.
        """
        return self.driver.execute_script("""
            return arguments[0].querySelector('[aria-label="Next"]') !== null
                || arguments[0].querySelector('[aria-label="Nächstes"]') !== null
                || arguments[0].querySelector('[aria-label="Weiter"]') !== null;
        """, article)

    def _handle_carousel(self, article):
        """Clicks through all carousel slides, dwelling on each.

        Handles each slide individually so _is_video_article only runs on the
        currently visible slide — ensuring video progress polling targets a
        playing video, not a hidden preloaded one.
        """
        slide_num = 0
        while True:
            is_video = self._is_video_article(article)
            self.logger.info(f"Carousel slide {slide_num}: {'video' if is_video else 'image'}")
            if is_video:
                self._wait_for_video_progress(article, self.video_watch_pct)
            else:
                time.sleep(random.uniform(1.5, 3.5))

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

        self.action_logger.log_action('carousel_viewed', {'slides_viewed': slide_num + 1})

    def _is_video_article(self, article) -> bool:
        """Returns True if the article's currently visible slide contains a playing video.

        Uses offsetWidth/offsetHeight rather than querySelector to exclude preloaded
        but hidden <video> elements that Instagram places in the DOM for all carousel
        slides simultaneously.
        """
        return self.driver.execute_script("""
            var videos = arguments[0].querySelectorAll('video');
            for (var v of videos) {
                if (v.offsetWidth > 0 && v.offsetHeight > 0) return true;
            }
            return false;
        """, article)

    def _wait_for_video_progress(self, article, target_pct: float, timeout: float = 60.0):
        """Blocks until the currently visible video has been watched to target_pct, or timeout is hit."""
        start = time.time()
        while time.time() - start < timeout:
            result = self.driver.execute_script("""
                var videos = arguments[0].querySelectorAll('video');
                for (var v of videos) {
                    if (v.offsetWidth > 0 && v.offsetHeight > 0 && v.duration) {
                        return {currentTime: v.currentTime, duration: v.duration};
                    }
                }
                return null;
            """, article)
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

    def initialize_browser(self):
        if self.use_virtual_display:
            self.display = Display(visible=0, size=(800, 600))
            self.display.start()
            self.logger.info(f"Virtual display started for {self.profile_id}")

        options = Options()

        firefox_profile = self.profile_config['firefox_profile']
        options.add_argument('-profile')
        options.add_argument(firefox_profile)

        user_agent = self.profile_config.get('user_agent',
            'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15')
        options.set_preference("general.useragent.override", user_agent)

        options.set_preference("layout.css.devPixelsPerPx", "1.0")
        options.set_preference("layout.viewport.width", "390")
        options.set_preference("layout.viewport.height", "844")

        options.set_preference("dom.webdriver.enabled", False)
        options.set_preference("useAutomationExtension", False)
        options.set_preference("privacy.trackingprotection.enabled", False)
        options.set_preference("network.http.referer.spoofSource", True)

        options.set_preference("permissions.default.image", 1)
        options.set_preference("browser.display.show_image_placeholders", True)

        seleniumwire_options = {
            'disable_encoding': True
        }

        self.driver = wire_webdriver.Firefox(
            service=Service('/usr/local/bin/geckodriver'),
            options=options,
            seleniumwire_options=seleniumwire_options
        )

        self.driver.set_window_size(500, 926)

        self.driver.execute_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'platform', {
                get: () => 'iPhone'
            });
            Object.defineProperty(navigator, 'oscpu', {
                get: () => undefined
            });
        """)

        self.logger.info(f"Browser initialized for {self.profile_id}")

    def collect_feed(self, target_posts: int = 50) -> List[Dict]:
        # 1. Generate session id, open DB, register session, instantiate archive + interceptor + metrics pusher.
        # Per decision: fail loudly if either the DB or Pushgateway is unreachable at session start.
        started_at = datetime.now()
        self.session_id = f"{self.profile_id}_{started_at.strftime('%Y%m%d_%H%M%S')}"

        self.db = DatabaseManager()
        self.db.connect()

        self.pusher = MetricsPusher(account_id=self.profile_id, session_id=self.session_id)
        self.pusher.connect()

        self.db.ensure_account(
            account_id=self.profile_id,
            email=self.profile_config['email'],
            firefox_profile=self.profile_config['firefox_profile'],
            role=self.profile_config['role'],          # 'study'
            bucket=self.profile_config.get('bucket'),  # None for study
            assigned_interests=self.profile_config.get('assigned_interests'),  # ['Control', 'Fit', 'BandF']
            gender=self.profile_config.get('gender'),  # 'F' or 'M'
            condition=self.profile_config.get('condition'),  # 'interaction' or 'no_interaction'
        )

        self.action_logger = ActionLogger(self.profile_id, session_id=self.session_id)
        self.archive = RawArchive(self.profile_id, self.session_id)
        self.interceptor = SeleniumWireInterceptor(archive=self.archive)

        session_duration_minutes = self.human_behavior.realistic_session_duration()
        session_end_time = time.time() + (session_duration_minutes * 60)

        self.db.insert_session(
            session_id=self.session_id,
            account_id=self.profile_id,
            started_at=started_at,
            planned_duration_seconds=int(session_duration_minutes * 60),
            target_posts=target_posts,
        )

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

            scroll_count = 0
            max_scrolls = 200

            while scroll_count < max_scrolls and time.time() < session_end_time:
                # 1. What is centered right now — before any scrolling
                article = self.update_context()
                post_link = (
                    (self.current_post_data.get('postLink') if self.current_post_data else None)
                    or (self._get_post_link_from_article(article) if article else None)
                    or ''
                )

                dwell_start = time.time()
                is_new_post = article is not None and post_link and post_link not in self.processed_posts

                # 2. Dwell on whatever is centered — skip if already processed
                if article is None or post_link in self.processed_posts:
                    time.sleep(random.uniform(0.3, 0.7))
                elif self._is_carousel_article(article):
                    if self.do_interact:
                        self._handle_carousel(article)
                    else:
                        time.sleep(random.uniform(1.5, 3.5))
                    self.processed_posts.add(post_link)
                    if self.human_behavior.should_like_post() and self.do_interact:
                        if post_link not in self.attempted_like_posts:
                            self.attempted_like_posts.add(post_link)
                            liked = self.perform_like_action(article)
                            if liked:
                                time.sleep(random.uniform(1.5, 3.0))
                elif self._is_video_article(article):
                    if self.do_interact:
                        self._wait_for_video_progress(article, self.video_watch_pct)
                    else:
                        time.sleep(random.uniform(2.0, 4.0))
                    self.processed_posts.add(post_link)
                    if self.human_behavior.should_like_post() and self.do_interact  :
                        if post_link not in self.attempted_like_posts:
                            self.attempted_like_posts.add(post_link)
                            liked = self.perform_like_action(article)
                            if liked:
                                time.sleep(random.uniform(1.5, 3.0))
                else:
                    if self.human_behavior.should_pause():
                        pause_duration = self.human_behavior.pause_duration()
                        self.action_logger.log_pause(duration=pause_duration)
                        time.sleep(pause_duration)
                        if self.human_behavior.should_like_post() and self.do_interact:
                            if post_link not in self.attempted_like_posts:
                                self.attempted_like_posts.add(post_link)
                                liked = self.perform_like_action(article)
                                if liked:
                                    time.sleep(random.uniform(1.5, 3.0))
                    self.processed_posts.add(post_link)

                # 3. Follow suggested accounts — runs for any centered article regardless of type
                if article is not None and self.profile_config.get('follow_suggested', False):
                    self.logger.info("Trying to follow")
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
                            self.perform_follow_action(article, username)

                # Log total dwell time for newly processed posts (all types)
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

                # Strategic mid-session push every 10 scrolls so a mid-run crash
                # still leaves observable metrics in Prometheus.
                if scroll_count % 10 == 0:
                    self.pusher.push()

            self.interceptor.process_requests(self.driver)
            network_posts = self.interceptor.get_posts()

            self.action_logger.log_api_intercept(
                endpoint='feed/timeline',
                posts_count=len(network_posts)
            )

            for i, post in enumerate(network_posts):
                post['profile_id'] = self.profile_id
                post['profile_email'] = self.profile_config['email']
                post['position'] = i + 1
                post['collection_timestamp'] = datetime.now().isoformat()

            self.logger.info(f"Collection complete: {len(network_posts)} posts")

            final_stats = {
                'posts_collected': len(network_posts),
                'scrolls_performed': scroll_count,
                'target_reached': len(network_posts) >= target_posts,
                'suggested_posts': sum(1 for p in network_posts if p.get('is_suggested', False)),
                'followed_posts': sum(1 for p in network_posts if p.get('is_following', False)),
                'followed_suggested': len(self.followed_accounts)
            }
            self.action_logger.log_session_end(final_stats)

            # Populate pusher with final counts. Counters were not incremented
            # during the loop because posts_collected is only known post-hoc
            # (interceptor deduplicates). This is the authoritative final count.
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
        # Final metrics push — fails loudly per decision.
        if self.pusher:
            duration = (ended_at - self.action_logger.session_start).total_seconds()
            self.pusher.finalize(duration_seconds=duration, status=status)

    def save_feed_data(self, data):
        """Legacy JSONL dump, kept as dead code. DB + raw archive are the source of truth."""
        if not data:
            self.logger.warning("No data to save")
            return None

        output_dir = Path('feed_data')
        output_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"feed_{self.profile_id}_{timestamp}.json"
        filepath = output_dir / filename

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        self.logger.info(f"Saved {len(data)} posts to {filename}")
        return str(filepath)

    def cleanup(self):
        if self.driver:
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