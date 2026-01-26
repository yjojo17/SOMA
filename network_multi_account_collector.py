"""
Multi-Account Instagram Collector with Network Interception + Human Behavior Emulation
Uses undetected-geckodriver with selenium-wire for stealth network interception
Includes comprehensive action logging and realistic human behavior patterns
"""
from seleniumwire import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.firefox import GeckoDriverManager
from pyvirtualdisplay import Display

from seleniumwire_interceptor import SeleniumWireInterceptor
from human_behavior import HumanBehavior
from action_logger import ActionLogger

import logging
import time
import random
import json
from datetime import datetime
from pathlib import Path


class NetworkMultiAccountBrowser:
    """Multi-account browser with network interception and stealth capabilities"""
    
    def __init__(self, account_name="account1", use_virtual_display=True):
        self.account_name = account_name
        self.use_virtual_display = use_virtual_display
        self.logger = self._setup_logging()
        self.driver = None
        self.display = None
        self.interceptor = SeleniumWireInterceptor()
        
        # Account configurations
        self.account_configs = {
            "account1": {
                "profile_path": "/home/yjojo/.mozilla/firefox/j0ic88lj.insta_research_test",
                "email": "yscheyjojoxD@gmail.com",
                "description": "Main research account"
            },
            "account2": {
                "profile_path": "/home/yjojo/.mozilla/firefox/yuodsiak.insta_research_test2", 
                "email": "yscheyjojoxD+insta2@gmail.com",
                "description": "Secondary research account"
            },
        }
    
    def _setup_logging(self):
        logger = logging.getLogger(f'network_instagram_{self.account_name}')
        logger.setLevel(logging.DEBUG)
        
        if logger.handlers:
            logger.handlers.clear()
        
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        file_handler = logging.FileHandler(f'instagram_network_{self.account_name}.log')
        file_handler.setLevel(logging.DEBUG)
        
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)
        
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
        logger.propagate = False
        
        return logger

    def _random_delay(self, min_seconds: float = 1.0, max_seconds: float = 3.0):
        time.sleep(random.uniform(min_seconds, max_seconds))

    def get_account_config(self):
        """Get configuration for current account"""
        if self.account_name not in self.account_configs:
            raise ValueError(f"Account '{self.account_name}' not configured")
        return self.account_configs[self.account_name]

    def setup_virtual_display(self):
        """Setup virtual display if requested"""
        if self.use_virtual_display:
            self.display = Display(visible=0, size=(1280, 1024))
            self.display.start()
            self.logger.info(f"Virtual display started for {self.account_name}")

    def setup_browser(self):
        """Setup Firefox with selenium-wire and anti-detection measures"""
        self.setup_virtual_display()
        
        options = Options()
        config = self.get_account_config()
        profile_path = config["profile_path"]
        
        if not Path(profile_path).exists():
            raise FileNotFoundError(f"Profile path does not exist: {profile_path}")
        
        self.logger.info(f"Using profile: {profile_path}")
        options.add_argument('-profile')
        options.add_argument(profile_path)
        
        # Mobile emulation
        mobile_settings = {
            "general.useragent.override": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/95.0.4638.54 Mobile/15E148 Safari/604.1",
            "layout.css.devPixelsPerPx": "1.0",
            "layout.viewport.width": "390",
            "layout.viewport.height": "844"
        }
        
        for pref, value in mobile_settings.items():
            options.set_preference(pref, value)
        
        # Anti-detection settings
        options.set_preference("dom.webdriver.enabled", False)
        options.set_preference("useAutomationExtension", False)
        options.set_preference("privacy.trackingprotection.enabled", False)
        options.set_preference("network.http.referer.spoofSource", True)
        
        # selenium-wire options (for network interception)
        seleniumwire_options = {
            'disable_encoding': True,
        }
        
        try:
            service = Service(GeckoDriverManager().install())
            self.driver = webdriver.Firefox(
                service=service,
                options=options,
                seleniumwire_options=seleniumwire_options
            )
            
            # Execute anti-detection script
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            
            self.driver.set_window_size(500, 926)
            self.driver.implicitly_wait(10)
            self.logger.info(f"Browser with network interception started for {self.account_name}")
            return self.driver
            
        except Exception as e:
            self.logger.error(f"Failed to start browser: {str(e)}")
            self.cleanup()
            raise

    def check_login_status(self):
        """Check if we're already logged in"""
        try:
            config = self.get_account_config()
            self.logger.info(f"Checking login for {self.account_name} ({config['email']})")
            
            self.driver.get('https://www.instagram.com/')
            self._random_delay()
            
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="username"]'))
                )
                self.logger.warning(f"{self.account_name} not logged in")
                return False
            except:
                self.logger.info(f"{self.account_name} already logged in")
                return True
                
        except Exception as e:
            self.logger.error(f"Error checking login: {str(e)}")
            return False

    def cleanup(self):
        """Close browser and virtual display"""
        if self.driver:
            self.driver.quit()
            self.logger.info(f"Browser closed for {self.account_name}")
        
        if self.display:
            self.display.stop()
            self.logger.info(f"Virtual display stopped for {self.account_name}")


class NetworkFeedCollector(NetworkMultiAccountBrowser):
    """
    Collector using network interception with human behavior emulation
    """
    
    def __init__(self, account_name="account1", use_virtual_display=True, use_html_fallback=True):
        super().__init__(account_name, use_virtual_display)
        self.use_html_fallback = use_html_fallback
        self.human_behavior = HumanBehavior(self.logger)
        self.action_logger = None  # Initialized per session
    
    def collect_feed_posts(self, target_posts: int = 20):
        """
        Collect feed posts using network interception with human-like behavior
        """
        config = self.get_account_config()
        
        # Initialize action logger for this session
        self.action_logger = ActionLogger(self.account_name)
        
        # Determine realistic session duration
        session_duration_minutes = self.human_behavior.realistic_session_duration()
        session_end_time = time.time() + (session_duration_minutes * 60)
        
        # Log session start
        self.action_logger.log_session_start({
            'target_posts': target_posts,
            'use_virtual_display': self.use_virtual_display,
            'planned_duration': session_duration_minutes
        })
        
        self.logger.info(f"Starting collection for {self.account_name} (target: {target_posts}, duration: {session_duration_minutes}min)")
        
        try:
            self.driver.get('https://www.instagram.com/')
            time.sleep(2)
            
            # Wait for feed
            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, 'article'))
                )
                self.logger.info("Feed loaded")
            except:
                self.logger.error("Feed failed to load")
                self.action_logger.log_error('feed_load_failed', 'Feed did not load within timeout')
                return []
            
            scroll_count = 0
            max_scrolls = 100
            
            # Main collection loop with human-like behavior
            while scroll_count < max_scrolls and time.time() < session_end_time:
                # Process network requests captured so far
                self.interceptor.process_requests(self.driver)
                posts_captured = len(self.interceptor.get_posts())
                
                self.logger.info(f"API posts: {posts_captured}/{target_posts}, scroll: {scroll_count}")
                
                if posts_captured >= target_posts:
                    break
                
                # Execute human-like scroll
                scroll_data = self.human_behavior.variable_scroll(self.driver)
                self.action_logger.log_scroll(scroll_data)
                
                # Variable delay after scroll
                base_delay = self.human_behavior.scroll_delay()
                
                # Random pause to "read content" 
                if self.human_behavior.should_pause():
                    pause_duration = self.human_behavior.pause_duration()
                    self.logger.debug(f"Pausing for {pause_duration:.2f}s")
                    self.action_logger.log_pause(pause_duration, 'content_viewing')
                    time.sleep(pause_duration)
                else:
                    time.sleep(base_delay)
                
                # Occasional mouse movement
                mouse_data = self.human_behavior.mouse_movement(self.driver)
                if mouse_data:
                    self.action_logger.log_mouse_move(mouse_data)
                
                # Rare backwards scroll (checking previous content)
                if self.human_behavior.should_back_scroll():
                    back_distance = self.human_behavior.back_scroll_distance(self.driver)
                    self.driver.execute_script(f"window.scrollBy(0, {back_distance});")
                    self.action_logger.log_action('back_scroll', {'distance_px': back_distance})
                    time.sleep(random.uniform(0.5, 1.5))
                
                scroll_count += 1
            
            # Final processing of all captured requests
            self.interceptor.process_requests(self.driver)
            network_posts = self.interceptor.get_posts()
            
            # Log API interception statistics
            self.action_logger.log_api_intercept(
                endpoint='feed/timeline',
                posts_count=len(network_posts)
            )
            
            # Add account metadata to posts
            for i, post in enumerate(network_posts):
                post['account_name'] = self.account_name
                post['account_email'] = config['email']
                post['position'] = i + 1
                post['collection_timestamp'] = datetime.now().isoformat()
                
                # Log each post view
                self.action_logger.log_post_view(
                    post_id=post.get('pk', f'unknown_{i}'),
                    post_data=post
                )
            
            self.logger.info(f"Network capture complete: {len(network_posts)} posts")
            
            # Log session end
            final_stats = {
                'posts_collected': len(network_posts),
                'scrolls_performed': scroll_count,
                'target_reached': len(network_posts) >= target_posts
            }
            self.action_logger.log_session_end(final_stats)
            
            # Save action logs
            self.action_logger.save_session()
            self.action_logger.print_summary()
            
            return network_posts[:target_posts]
            
        except Exception as e:
            self.logger.error(f"Error in collection: {str(e)}")
            self.action_logger.log_error('collection_error', str(e))
            self.action_logger.save_session()
            return []
    
    def save_feed_data(self, data):
        """Save feed data"""
        if not data:
            self.logger.warning("No data to save")
            return
            
        output_dir = Path('feed_data')
        output_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"feed_posts_network_{self.account_name}_{timestamp}.json"
        
        with open(output_dir / filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        self.logger.info(f"Saved {len(data)} posts to {filename}")
        return filename


def main():
    """Test the network-based collector with human behavior"""
    logging.basicConfig(level=logging.INFO)
    
    collector = NetworkFeedCollector(
        account_name="account1",
        use_virtual_display=False  # Set True for headless
    )
    
    try:
        collector.setup_browser()
        
        if collector.check_login_status():
            posts = collector.collect_feed_posts(target_posts=20)
            if posts:
                collector.save_feed_data(posts)
                print(f"\n✓ Success! Collected {len(posts)} posts from network API")
                print(f"✓ Human behavior patterns applied")
                print(f"✓ Action logs saved")
            else:
                print("\n✗ No posts collected - check logs")
        else:
            print("Please log in manually first")
    
    finally:
        collector.cleanup()


if __name__ == "__main__":
    main()