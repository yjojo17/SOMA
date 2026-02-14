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
        self.account_name = account_name
        self.session_id = session_id or f"{account_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.session_start = datetime.now()
        self.actions = []
        self.current_post_context = None
        self.logger = logging.getLogger(f'action_logger_{account_name}')

        self.stats = {
            'scrolls': 0,
            'pauses': 0,
            'mouse_moves': 0,
            'api_calls': 0,
            'posts_viewed': 0
        }

    def set_active_post(self, post_data: Optional[Dict]):
        """Call this whenever the bot identifies a new post in the viewport center."""
        self.current_post_context = post_data
        if post_data:
            self.stats['posts_viewed'] += 1

    def log_action(self, action_type: str, details: Dict[str, Any]):
        """Log any interaction, always stamping the currently active post context."""
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'session_id': self.session_id,
            'account': self.account_name,
            'action_type': action_type,
            'details': details,
            'post_context': self.current_post_context or {}
        }

        self.actions.append(log_entry)

        if action_type == 'scroll':
            self.stats['scrolls'] += 1
        elif action_type == 'pause':
            self.stats['pauses'] += 1
        elif action_type == 'mouse_move':
            self.stats['mouse_moves'] += 1
        elif action_type == 'api_intercept':
            self.stats['api_calls'] += 1

        self.logger.debug(f"{action_type}: {details}")

    def log_scroll(self, scroll_data: Dict, scroll_position: int = 0):
        scroll_data['scroll_position_px'] = scroll_position
        self.log_action('scroll', scroll_data)

    def log_pause(self, duration: float, reason: str = 'content_viewing'):
        self.log_action('pause', {'duration': duration, 'reason': reason})

    def log_like(self):
        """Logs a like for the current active post."""
        if self.current_post_context:
            self.log_action('like', {
                'post_id': self.current_post_context.get('pk'),
                'username': self.current_post_context.get('profile_name')
            })

    def log_mouse_move(self, movement_data: Dict):
        self.log_action('mouse_move', movement_data)

    def log_api_intercept(self, endpoint: str, posts_count: int, response_size: Optional[int] = None):
        self.log_action('api_intercept', {
            'endpoint': endpoint,
            'posts_count': posts_count,
            'response_size': response_size
        })

    def log_post_view(self, post_id: str, post_data: Dict):
        self.stats['posts_viewed'] += 1
        self.log_action('post_view', {
            'post_id': post_id,
            'post_type': post_data.get('media_type', 'unknown'),
            'has_video': post_data.get('has_video', False),
            'carousel_count': len(post_data.get('carousel_media', []))
        })

    def log_session_start(self, config: Dict):
        self.log_action('session_start', {
            'target_posts': config.get('target_posts'),
            'use_virtual_display': config.get('use_virtual_display'),
            'planned_duration': config.get('planned_duration')
        })

    def log_session_end(self, final_stats: Dict):
        session_duration = (datetime.now() - self.session_start).total_seconds()
        self.log_action('session_end', {
            'session_duration_seconds': session_duration,
            'session_duration_minutes': session_duration / 60,
            'total_actions': len(self.actions),
            'final_stats': final_stats
        })

    def log_error(self, error_type: str, error_message: str, context: Optional[Dict] = None):
        self.log_action('error', {
            'error_type': error_type,
            'error_message': error_message,
            'context': context or {}
        })

    def get_statistics(self) -> Dict:
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
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        actions_file = output_path / f'{self.session_id}_actions.jsonl'
        with open(actions_file, 'w') as f:
            for action in self.actions:
                f.write(json.dumps(action) + '\n')

        stats_file = output_path / f'{self.session_id}_stats.json'
        with open(stats_file, 'w') as f:
            json.dump(self.get_statistics(), f, indent=2)

        self.logger.info(f"Session logs saved: {actions_file}")
        self.logger.info(f"Session stats saved: {stats_file}")

        return str(actions_file)

    def get_recent_actions(self, n: int = 10) -> List[Dict]:
        return self.actions[-n:] if self.actions else []

    def get_actions_by_type(self, action_type: str) -> List[Dict]:
        return [action for action in self.actions if action['action_type'] == action_type]

    def print_summary(self):
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