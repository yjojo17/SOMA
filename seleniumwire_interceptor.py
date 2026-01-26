"""
Instagram Network Interceptor for selenium-wire
Works with Firefox profiles - extracts data from network API calls
"""
import json
import re
from datetime import datetime
from typing import Optional, Dict, List
import logging


class SeleniumWireInterceptor:
    """Intercepts Instagram API calls using selenium-wire"""
    
    def __init__(self):
        self.logger = logging.getLogger('instagram_network')
        self.captured_posts = []
        self.seen_post_links = set()  # Track unique posts by link
        
        # Instagram API endpoints to monitor
        self.api_patterns = [
            r'/api/v1/feed/timeline',
            r'/api/v1/feed/user/',
            r'/api/graphql',  # Main GraphQL endpoint for feed
            r'/graphql/query',
            r'/api/v1/media/.*/info',
        ]
    
    def process_requests(self, driver):
        """Process all captured requests from selenium-wire"""
        for request in driver.requests:
            if request.response:
                url = request.url
                
                # Check if this is an Instagram API call
                if self._is_instagram_api(url):
                    try:
                        # Get response body
                        body = request.response.body
                        if body:
                            data = json.loads(body.decode('utf-8'))
                            self._process_api_response(url, data)
                    except Exception as e:
                        self.logger.debug(f"Error processing {url}: {e}")
    
    def _is_instagram_api(self, url: str) -> bool:
        """Check if URL matches Instagram API patterns"""
        return any(re.search(pattern, url) for pattern in self.api_patterns)
    
    def _process_api_response(self, url: str, data: Dict):
        """Process Instagram API response and extract post data"""
        # Timeline feed
        if '/feed/timeline' in url or '/feed/user' in url:
            self._extract_timeline_posts(data)
        
        # GraphQL query (both /api/graphql and /graphql/query)
        elif '/graphql' in url:
            self._extract_graphql_posts(data)
        
        # Individual media info
        elif '/media/' in url and 'info' in url:
            self._extract_media_info(data)
    
    def _extract_timeline_posts(self, data: Dict):
        """Extract posts from timeline feed response"""
        try:
            # Try different possible structures
            items = (data.get('feed_items') or 
                    data.get('items') or 
                    data.get('data', {}).get('user', {}).get('edge_owner_to_timeline_media', {}).get('edges', []))
            
            if not items:
                return
            
            for item in items:
                # Handle different item structures
                media = None
                
                if 'media_or_ad' in item:
                    media = item['media_or_ad']
                elif 'node' in item:
                    media = item['node']
                elif 'media' in item:
                    media = item['media']
                else:
                    media = item
                
                if media:
                    post_data = self._parse_media_object(media)
                    if post_data:
                        post_link = post_data.get('postLink')
                        if post_link and post_link not in self.seen_post_links:
                            self.seen_post_links.add(post_link)
                            self.captured_posts.append(post_data)
                            self.logger.info(f"Captured post from API: {post_link}")
        
        except Exception as e:
            self.logger.error(f"Error extracting timeline posts: {e}")
    
    def _extract_graphql_posts(self, data: Dict):
        """Extract posts from GraphQL response"""
        try:
            # Handle xdt_api__v1__feed__timeline__connection (main feed endpoint)
            if 'xdt_api__v1__feed__timeline__connection' in data.get('data', {}):
                feed_connection = data['data']['xdt_api__v1__feed__timeline__connection']
                edges = feed_connection.get('edges', [])
                
                for edge in edges:
                    # Structure is: edge -> node -> media
                    node = edge.get('node', {})
                    media = node.get('media')
                    
                    # Skip if no media (ads, suggestions, etc.)
                    if not media:
                        continue
                    
                    post_data = self._parse_media_object(media)
                    if post_data:
                        post_link = post_data.get('postLink')
                        if post_link and post_link not in self.seen_post_links:
                            self.seen_post_links.add(post_link)
                            self.captured_posts.append(post_data)
                            self.logger.info(f"Captured post from GraphQL: {post_link}")
                
                return  # Successfully processed
            
            # Fallback: Handle edge_owner_to_timeline_media structure
            edges = data.get('data', {}).get('user', {}).get('edge_owner_to_timeline_media', {}).get('edges', [])
            
            for edge in edges:
                node = edge.get('node', {})
                post_data = self._parse_media_object(node)
                if post_data:
                    post_link = post_data.get('postLink')
                    if post_link and post_link not in self.seen_post_links:
                        self.seen_post_links.add(post_link)
                        self.captured_posts.append(post_data)
        
        except Exception as e:
            self.logger.error(f"Error extracting GraphQL posts: {e}")
    
    def _extract_media_info(self, data: Dict):
        """Extract individual media info"""
        try:
            items = data.get('items', [])
            for item in items:
                post_data = self._parse_media_object(item)
                if post_data:
                    post_link = post_data.get('postLink')
                    if post_link and post_link not in self.seen_post_links:
                        self.seen_post_links.add(post_link)
                        self.captured_posts.append(post_data)
        except Exception as e:
            self.logger.error(f"Error extracting media info: {e}")
    
    def _parse_media_object(self, media: Dict) -> Optional[Dict]:
        """
        Parse Instagram media object to our data schema
        Maps Instagram API response to the same format as HTML scraper
        """
        try:
            post_data = {
                'collected_at': datetime.now().isoformat(),
                'api_source': True,
                'postLink': self._get_post_link(media),
                'timestamp': self._get_timestamp(media),
                'profile_name': self._get_username(media),
                'profile_url': self._get_profile_url(media),
                'is_verified': self._is_verified(media),
                'likes': self._get_like_count(media),
                'description': self._get_caption(media),
                'media_type': media.get('media_type', media.get('__typename')),
            }
            
            # Skip if no valid post link
            if not post_data['postLink']:
                return None
            
            # Suggested content detection
            post_data['content_type'] = {
                'is_suggested': media.get('is_suggested_user', False),
                'from_followed_account': not media.get('is_suggested_user', False),
                'follow_action_taken': None,
                'follow_timestamp': None
            }
            
            return post_data
            
        except Exception as e:
            self.logger.debug(f"Error parsing media object: {e}")
            return None
    
    def _get_post_link(self, media: Dict) -> str:
        """Extract post link"""
        code = media.get('code') or media.get('shortcode')
        if code:
            return f"https://www.instagram.com/p/{code}/"
        
        # Try to get from pk/id
        pk = media.get('pk') or media.get('id')
        if pk:
            return f"https://www.instagram.com/p/{pk}/"
        
        return ""
    
    def _get_timestamp(self, media: Dict) -> Optional[str]:
        """Extract timestamp"""
        taken_at = media.get('taken_at') or media.get('taken_at_timestamp')
        if taken_at:
            return str(taken_at)
        return None
    
    def _get_username(self, media: Dict) -> str:
        """Extract username"""
        user = media.get('user', {})
        return user.get('username', '') or user.get('name', '')
    
    def _get_profile_url(self, media: Dict) -> str:
        """Extract profile URL"""
        username = self._get_username(media)
        if username:
            return f"https://www.instagram.com/{username}/"
        return ""
    
    def _is_verified(self, media: Dict) -> bool:
        """Check if user is verified"""
        user = media.get('user', {})
        return user.get('is_verified', False)
    
    def _get_like_count(self, media: Dict) -> str:
        """Extract like count"""
        like_count = (media.get('like_count') or 
                     media.get('edge_liked_by', {}).get('count') or 
                     media.get('edge_media_preview_like', {}).get('count') or 
                     0)
        
        # Format similar to HTML scraper output
        if like_count >= 1000000:
            return f"{like_count / 1000000:.1f}M"
        elif like_count >= 1000:
            return f"{like_count / 1000:.1f}K"
        return str(like_count)
    
    def _get_caption(self, media: Dict) -> str:
        """Extract caption/description"""
        # Try different caption structures
        caption = media.get('caption')
        
        if caption:
            if isinstance(caption, dict):
                return caption.get('text', '')
            elif isinstance(caption, str):
                return caption
        
        # Try edge structure (GraphQL)
        edge_caption = media.get('edge_media_to_caption', {}).get('edges', [])
        if edge_caption and len(edge_caption) > 0:
            return edge_caption[0].get('node', {}).get('text', '')
        
        return ""
    
    def get_posts(self) -> List[Dict]:
        """Get all captured posts"""
        return self.captured_posts
    
    def clear(self):
        """Clear captured data"""
        self.captured_posts = []
        self.seen_post_links = set()