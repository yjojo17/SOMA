"""
Human Behavior Emulation Module
Centralizes all human-like interaction patterns for Instagram automation
"""
import random
import time
from datetime import datetime
from typing import Dict, Optional, Tuple
import logging


class HumanBehavior:
    """Emulates realistic human browsing behavior patterns"""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.session_start = datetime.now()
        
    def scroll_delay(self) -> float:
        """
        Variable delay between scrolls with realistic distribution
        
        Returns:
            float: Delay in seconds (0.5-5s, weighted toward 1-2.5s)
        """
        delays = [
            random.uniform(0.5, 1.0),   # Quick scroll: 20%
            random.uniform(1.0, 2.5),   # Normal scroll: 60%  
            random.uniform(2.5, 5.0)    # Pause to read: 20%
        ]
        weights = [0.2, 0.6, 0.2]
        return random.choices(delays, weights=weights)[0]
    
    def content_dwell_time(self, post_type: str = 'image') -> float:
        """
        Realistic viewing time based on content type
        
        Args:
            post_type: Type of content (image, carousel, video, reel)
            
        Returns:
            float: Dwell time in seconds
        """
        dwell_times = {
            'image': (1.5, 4.0),
            'carousel': (3.0, 8.0),
            'video': (2.0, 15.0),
            'reel': (3.0, 30.0)
        }
        min_t, max_t = dwell_times.get(post_type, (1.0, 3.0))
        return random.uniform(min_t, max_t)
    
    def variable_scroll(self, driver) -> Dict:
        """
        Execute scroll with acceleration/deceleration for natural movement
        
        Args:
            driver: Selenium WebDriver instance
            
        Returns:
            dict: Scroll metadata (distance, steps, viewport)
        """
        viewport_h = driver.execute_script("return window.innerHeight;")
        
        # Scroll distance: 30-90% of viewport height
        scroll_percent = random.uniform(0.3, 0.9)
        distance = int(viewport_h * scroll_percent)
        
        # Multi-step scroll with easing (more human-like)
        steps = random.randint(3, 7)
        step_size = distance // steps
        
        for i in range(steps):
            driver.execute_script(f"window.scrollBy(0, {step_size});")
            time.sleep(random.uniform(0.05, 0.15))
        
        return {
            'distance_px': distance,
            'steps': steps,
            'viewport_height': viewport_h,
            'scroll_percent': scroll_percent
        }
    
    def should_pause(self) -> bool:
        """Random pause probability: 15%"""
        return random.random() < 0.15
    
    def should_hover(self) -> bool:
        """Random hover probability: 10%"""
        return random.random() < 0.10
    
    def should_back_scroll(self) -> bool:
        """Occasional backwards scroll: 5%"""
        return random.random() < 0.05
    
    def should_like_post(self) -> bool:
        """
        Random chance to like a post (10%)
        Simulates natural engagement behavior
        """
        return random.random() < 0.10
    
    def like_delay(self) -> float:
        """
        Delay before/after liking (0.3-1.5s)
        Quick action but not instant
        """
        return random.uniform(0.3, 1.5)
        
    def mouse_movement(self, driver, element=None) -> Optional[Dict]:
        """
        Occasional random mouse movement to simulate natural browsing
        
        Args:
            driver: Selenium WebDriver instance
            element: Optional element to move to
            
        Returns:
            dict: Mouse movement metadata or None if no movement
        """
        if not self.should_hover():
            return None
            
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            actions = ActionChains(driver)
            
            if element:
                # Move to specific element
                actions.move_to_element(element).perform()
                return {'type': 'element_hover', 'element': str(element)}
            else:
                # Random viewport movement
                viewport_w = driver.execute_script("return window.innerWidth;")
                viewport_h = driver.execute_script("return window.innerHeight;")
                
                x = random.randint(100, min(400, viewport_w - 100))
                y = random.randint(100, min(600, viewport_h - 100))
                
                actions.move_by_offset(x, y).perform()
                return {'type': 'random_movement', 'x': x, 'y': y}
                
        except Exception as e:
            self.logger.debug(f"Mouse movement failed: {e}")
            return None
    
    def realistic_session_duration(self) -> int:
        """
        Generate realistic session length
        
        Returns:
            int: Session duration in minutes (5-30min, weighted toward 10-15min)
        """
        durations = [
            random.randint(5, 10),   # Short session: 20%
            random.randint(10, 15),  # Normal session: 60%
            random.randint(15, 30)   # Long session: 20%
        ]
        weights = [0.2, 0.6, 0.2]
        return random.choices(durations, weights=weights)[0]
    
    def pause_duration(self) -> float:
        """
        Generate realistic pause duration when "reading" content
        
        Returns:
            float: Pause duration in seconds (2-8s)
        """
        return random.uniform(2.0, 8.0)
    
    def back_scroll_distance(self, driver) -> int:
        """
        Small backwards scroll to re-check content
        
        Args:
            driver: Selenium WebDriver instance
            
        Returns:
            int: Negative scroll distance in pixels
        """
        viewport_h = driver.execute_script("return window.innerHeight;")
        # Small backwards scroll: 10-30% of viewport
        return -random.randint(int(viewport_h * 0.1), int(viewport_h * 0.3))
    
    def interaction_delay(self, min_seconds: float = 0.5, max_seconds: float = 2.0) -> float:
        """
        Generic delay between any two actions
        
        Args:
            min_seconds: Minimum delay
            max_seconds: Maximum delay
            
        Returns:
            float: Delay in seconds
        """
        return random.uniform(min_seconds, max_seconds)
    
    def get_session_stats(self) -> Dict:
        """
        Get current session statistics
        
        Returns:
            dict: Session metadata
        """
        session_duration = (datetime.now() - self.session_start).total_seconds()
        return {
            'session_start': self.session_start.isoformat(),
            'session_duration_seconds': session_duration,
            'session_duration_minutes': session_duration / 60
        }