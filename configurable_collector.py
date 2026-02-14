"""
Configurable Multi-Account Instagram Feed Collector
Clean version with suggested post detection
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
from seleniumwire import webdriver as wire_webdriver

from seleniumwire_interceptor import SeleniumWireInterceptor
from human_behavior import HumanBehavior
from action_logger import ActionLogger


class ConfigurableNetworkCollector:
    
    def __init__(self, profile_id: str, config_file: str = 'research_config.yaml', use_virtual_display: bool = False):
        self.profile_id = profile_id
        self.config_file = config_file
        self.use_virtual_display = use_virtual_display
        
        self.config = self._load_config()
        self.profile_config = self._get_profile_config(profile_id)
        
        self.logger = self._setup_logging()
        
        self.display = None
        self.driver = None
        self.interceptor = SeleniumWireInterceptor()
        self.human_behavior = HumanBehavior(self.logger)
        self.action_logger = None
        
        self.current_scroll_position = 0
        
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
            # Get position of the article relative to the viewport
            position = self.driver.execute_script(
                "return arguments[0].getBoundingClientRect();", article
            )
            
            # If the top of the post is in the middle 50% of the screen, it's the active post
            if 0 <= position['top'] <= (viewport_height * 0.5):
                # Map this to your intercepted posts (this assumes linear order)
                network_posts = self.interceptor.get_posts()
                if index < len(network_posts):
                    return network_posts[index]
        return None

    def _get_post_link_from_article(self, article) -> Optional[str]:
        """Extracts the canonical post URL from a DOM article element."""
        try:
            link_elem = article.find_element(By.XPATH, ".//a[contains(@href, '/p/')]")
            return link_elem.get_attribute('href').split('?')[0]
        except Exception:
            return None

    def update_context(self):
        """Syncs network data and sets the active post by matching DOM link to intercepted data."""
        self.interceptor.process_requests(self.driver)
        captured_posts = self.interceptor.get_posts()

        articles = self.driver.find_elements(By.TAG_NAME, 'article')
        viewport_h = self.driver.execute_script("return window.innerHeight;")
        center = viewport_h / 2

        for article in articles:
            rect = self.driver.execute_script("return arguments[0].getBoundingClientRect();", article)
            if rect['top'] < center < rect['bottom']:
                link = self._get_post_link_from_article(article)
                if link:
                    for post_data in captured_posts:
                        if post_data.get('postLink') == link:
                            self.action_logger.set_active_post(post_data)
                            return True
                # Article is in view but no link match found yet (data not yet intercepted)
                self.action_logger.set_active_post(None)
                return False

        self.action_logger.set_active_post(None)
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
        
        proxy_id = self.profile_config.get('proxy')
        if proxy_id and proxy_id in self.config.get('proxies', {}):
            proxy_config = self.config['proxies'][proxy_id]
            seleniumwire_options['proxy'] = {
                'http': f'http://{proxy_config["credentials"]["username"]}:{proxy_config["credentials"]["password"]}@{proxy_config["host"]}:{proxy_config["port"]}',
                'https': f'https://{proxy_config["credentials"]["username"]}:{proxy_config["credentials"]["password"]}@{proxy_config["host"]}:{proxy_config["port"]}',
                'no_proxy': 'localhost,127.0.0.1'
            }
            self.logger.info(f"Using proxy: {proxy_config['location']}")
        
        self.driver = wire_webdriver.Firefox(
            options=options,
            seleniumwire_options=seleniumwire_options
        )
        
        self.driver.set_window_size(500, 926)
        
        self.driver.execute_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        self.logger.info(f"Browser initialized for {self.profile_id}")
    
    def collect_feed(self, target_posts: int = 50) -> List[Dict]:
        self.action_logger = ActionLogger(self.profile_id)
        
        session_duration_minutes = self.human_behavior.realistic_session_duration()
        session_end_time = time.time() + (session_duration_minutes * 60)
        
        self.action_logger.log_session_start({
            'target_posts': target_posts,
            'profile_id': self.profile_id,
            'region': self.profile_config['region'],
            'use_virtual_display': self.use_virtual_display,
            'planned_duration': session_duration_minutes
        })
        
        self.logger.info(f"Collection started: {self.profile_id} (target: {target_posts}, duration: {session_duration_minutes}min)")
        
        try:
            self.driver.get('https://www.instagram.com/')
            # Wait for first content
            WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'article')))
            
            scroll_count = 0
            max_scrolls = 50
            
            while scroll_count < max_scrolls and time.time() < session_end_time:
                # 1. Update the state: Find what post is in the center of the viewport
                scroll_data = self.human_behavior.variable_scroll(self.driver)
                self.current_scroll_position = self.driver.execute_script("return window.pageYOffset;")


                self.update_context()
                self.action_logger.log_scroll(scroll_data, self.current_scroll_position)


                # self.interceptor.process_requests(self.driver)
                # posts_captured = len(self.interceptor.get_posts())

                # self.logger.info(f"Posts: {posts_captured}/{target_posts}, scroll: {scroll_count}")
                
                # if posts_captured >= target_posts:
                #    break
                                
                # 2. Log scroll: post_context=None tells the logger to use self.current_post_context

                
                #base_delay = self.human_behavior.scroll_delay()
                
                # Capture Viewport Dwell Time (Key Personalization Factor)
                if self.human_behavior.should_pause():
                    pause_duration = self.human_behavior.pause_duration()
                    self.action_logger.log_pause(duration=pause_duration)
                    time.sleep(pause_duration)

                # Check if we've reached the target
                if len(self.interceptor.get_posts()) >= target_posts:
                    break
                
                # self.update_context()
                mouse_data = self.human_behavior.mouse_movement(self.driver)
                if mouse_data:
                    self.action_logger.log_mouse_move(mouse_data)
                
                if self.human_behavior.should_back_scroll():
                    back_distance = self.human_behavior.back_scroll_distance(self.driver)
                    self.driver.execute_script(f"window.scrollBy(0, {back_distance});")
                    self.update_context()
                    self.action_logger.log_action('back_scroll', {'distance_px': back_distance})
                    time.sleep(random.uniform(0.5, 1.5))
                
                time.sleep(self.human_behavior.scroll_delay())  # ← always fires, every cycle
                scroll_count += 1
            
            self.interceptor.process_requests(self.driver)
            network_posts = self.interceptor.get_posts()
            
            self.action_logger.log_api_intercept(
                endpoint='feed/timeline',
                posts_count=len(network_posts)
            )
            
            for i, post in enumerate(network_posts):
                post['profile_id'] = self.profile_id
                post['profile_email'] = self.profile_config['email']
                post['region'] = self.profile_config['region']
                post['position'] = i + 1
                post['collection_timestamp'] = datetime.now().isoformat()
                
                self.action_logger.log_post_view(
                    post_id=post.get('pk', f'unknown_{i}'),
                    post_data=post
                )
            
            self.logger.info(f"Collection complete: {len(network_posts)} posts")
            
            final_stats = {
                'posts_collected': len(network_posts),
                'scrolls_performed': scroll_count,
                'target_reached': len(network_posts) >= target_posts,
                'suggested_posts': sum(1 for p in network_posts if p.get('is_suggested', False)),
                'followed_posts': sum(1 for p in network_posts if p.get('is_following', False))
            }
            self.action_logger.log_session_end(final_stats)
            
            self.action_logger.save_session()
            self.action_logger.print_summary()
            
            return network_posts[:target_posts]
            
        except Exception as e:
            self.logger.error(f"Collection error: {str(e)}")
            self.action_logger.log_error('collection_error', str(e))
            self.action_logger.save_session()
            return []
    
    def save_feed_data(self, data):
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
                filepath = collector.save_feed_data(feed_data)
                results[profile_id] = {
                    'success': True,
                    'posts_collected': len(feed_data),
                    'suggested_count': sum(1 for p in feed_data if p.get('is_suggested', False)),
                    'followed_count': sum(1 for p in feed_data if p.get('is_following', False)),
                    'file': filepath
                }
                print(f"\n✓ {profile_id}: {len(feed_data)} posts collected")
                print(f"  Suggested: {results[profile_id]['suggested_count']}")
                print(f"  Followed: {results[profile_id]['followed_count']}")
                print(f"  File: {filepath}")
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
            print(f"✓ {profile_id}: {result['posts_collected']} posts " +
                  f"(Suggested: {result['suggested_count']}, Followed: {result['followed_count']})")
        else:
            print(f"✗ {profile_id}: {result.get('error', 'Unknown error')}")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()