"""
Action Logger Module
Comprehensive logging of all user interactions for behavioral analysis
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Any


class ActionLogger:
    """Structured action logging for behavioral pattern analysis"""
    
    def __init__(self, account_name: str, session_id: Optional[str] = None):
        """
        Initialize action logger
        
        Args:
            account_name: Name of the account being used
            session_id: Optional session ID, auto-generated if not provided
        """
        self.account_name = account_name
        self.session_id = session_id or f"{account_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.session_start = datetime.now()
        self.actions = []
        self.logger = logging.getLogger(f'action_logger_{account_name}')
        
        # Statistics
        self.stats = {
            'scrolls': 0,
            'pauses': 0,
            'mouse_moves': 0,
            'api_calls': 0,
            'posts_viewed': 0
        }
        
    def log_action(self, 
                   action_type: str, 
                   details: Dict[str, Any], 
                   post_context: Optional[Dict] = None):
        """
        Log any interaction with full context
        
        Args:
            action_type: Type of action (scroll, pause, hover, api_call, etc)
            details: Action-specific details
            post_context: Context of current post in view (if applicable)
        """
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'session_id': self.session_id,
            'account': self.account_name,
            'action_type': action_type,
            'details': details,
            'post_context': post_context or {}
        }
        
        self.actions.append(log_entry)
        
        # Update statistics
        if action_type in self.stats:
            self.stats[action_type] += 1
        elif action_type == 'scroll':
            self.stats['scrolls'] += 1
        elif action_type == 'pause':
            self.stats['pauses'] += 1
        elif action_type == 'mouse_move':
            self.stats['mouse_moves'] += 1
        elif action_type == 'api_intercept':
            self.stats['api_calls'] += 1
            
        self.logger.debug(f"{action_type}: {details}")
    
    def log_scroll(self, scroll_data: Dict, post_id: Optional[str] = None):
        """Log a scroll action"""
        self.log_action('scroll', scroll_data, {'post_id': post_id})
    
    def log_pause(self, duration: float, reason: str = 'content_viewing', post_id: Optional[str] = None):
        """Log a pause/dwell action"""
        self.log_action('pause', {
            'duration': duration,
            'reason': reason
        }, {'post_id': post_id})
    
    def log_mouse_move(self, movement_data: Dict, post_id: Optional[str] = None):
        """Log a mouse movement"""
        self.log_action('mouse_move', movement_data, {'post_id': post_id})
    
    def log_api_intercept(self, endpoint: str, posts_count: int, response_size: Optional[int] = None):
        """Log an API call interception"""
        self.log_action('api_intercept', {
            'endpoint': endpoint,
            'posts_count': posts_count,
            'response_size': response_size
        })
    
    def log_post_view(self, post_id: str, post_data: Dict):
        """Log when a post comes into view"""
        self.stats['posts_viewed'] += 1
        self.log_action('post_view', {
            'post_id': post_id,
            'post_type': post_data.get('media_type', 'unknown'),
            'has_video': post_data.get('has_video', False),
            'carousel_count': len(post_data.get('carousel_media', []))
        }, {'post_id': post_id})
    
    def log_session_start(self, config: Dict):
        """Log session start with configuration"""
        self.log_action('session_start', {
            'target_posts': config.get('target_posts'),
            'use_virtual_display': config.get('use_virtual_display'),
            'planned_duration': config.get('planned_duration')
        })
    
    def log_session_end(self, final_stats: Dict):
        """Log session end with final statistics"""
        session_duration = (datetime.now() - self.session_start).total_seconds()
        
        self.log_action('session_end', {
            'session_duration_seconds': session_duration,
            'session_duration_minutes': session_duration / 60,
            'total_actions': len(self.actions),
            'final_stats': final_stats
        })
    
    def log_error(self, error_type: str, error_message: str, context: Optional[Dict] = None):
        """Log an error occurrence"""
        self.log_action('error', {
            'error_type': error_type,
            'error_message': error_message,
            'context': context or {}
        })
    
    def get_statistics(self) -> Dict:
        """
        Get current session statistics
        
        Returns:
            dict: Comprehensive session statistics
        """
        session_duration = (datetime.now() - self.session_start).total_seconds()
        
        return {
            'session_id': self.session_id,
            'account': self.account_name,
            'session_start': self.session_start.isoformat(),
            'session_duration_seconds': session_duration,
            'session_duration_minutes': session_duration / 60,
            'total_actions': len(self.actions),
            'action_breakdown': self.stats.copy(),
            'actions_per_minute': len(self.actions) / (session_duration / 60) if session_duration > 0 else 0
        }
    
    def save_session(self, output_dir: str = 'action_logs') -> str:
        """
        Save session log to file
        
        Args:
            output_dir: Directory to save logs
            
        Returns:
            str: Path to saved file
        """
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)
        
        # Save actions as JSONL (one JSON object per line)
        actions_file = output_path / f'{self.session_id}_actions.jsonl'
        with open(actions_file, 'w') as f:
            for action in self.actions:
                f.write(json.dumps(action) + '\n')
        
        # Save statistics summary as JSON
        stats_file = output_path / f'{self.session_id}_stats.json'
        with open(stats_file, 'w') as f:
            json.dump(self.get_statistics(), f, indent=2)
        
        self.logger.info(f"Session logs saved: {actions_file}")
        self.logger.info(f"Session stats saved: {stats_file}")
        
        return str(actions_file)
    
    def get_recent_actions(self, n: int = 10) -> List[Dict]:
        """
        Get the most recent n actions
        
        Args:
            n: Number of recent actions to retrieve
            
        Returns:
            list: Recent action log entries
        """
        return self.actions[-n:] if self.actions else []
    
    def get_actions_by_type(self, action_type: str) -> List[Dict]:
        """
        Get all actions of a specific type
        
        Args:
            action_type: Type of action to filter
            
        Returns:
            list: Filtered action log entries
        """
        return [action for action in self.actions if action['action_type'] == action_type]
    
    def print_summary(self):
        """Print a human-readable summary of the session"""
        stats = self.get_statistics()
        
        print("\n" + "="*60)
        print(f"SESSION SUMMARY: {self.account_name}")
        print("="*60)
        print(f"Session ID: {self.session_id}")
        print(f"Duration: {stats['session_duration_minutes']:.2f} minutes")
        print(f"Total Actions: {stats['total_actions']}")
        print(f"Actions/Minute: {stats['actions_per_minute']:.2f}")
        print("\nAction Breakdown:")
        for action_type, count in stats['action_breakdown'].items():
            print(f"  {action_type}: {count}")
        print("="*60 + "\n")