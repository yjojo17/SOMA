"""
Instagram Network Interceptor for selenium-wire
"""
import json
import re
from datetime import datetime
from typing import Optional, Dict, List
import logging


class SeleniumWireInterceptor:

    def __init__(self, archive=None):
        """
        archive: optional RawArchive instance. If provided, every parsed API response
                 body is appended to the archive for later replay.
        """
        self.logger = logging.getLogger('instagram_network')
        self.captured_posts = []
        self.seen_post_links = set()
        self.seen_request_ids = set()
        self.archive = archive

        self.api_patterns = [
            r'/api/v1/feed/timeline',
            r'/api/v1/feed/user/',
            r'/api/graphql',
            r'/graphql/query',
            r'/api/v1/media/.*/info',
        ]

    def process_requests(self, driver):
        for request in driver.requests:
            if request.response:
                url = request.url

                if self._is_instagram_api(url):
                    # Dedupe so we don't re-process or re-archive the same response
                    # on repeat process_requests calls during the scroll loop.
                    req_id = getattr(request, 'id', None) or id(request)
                    if req_id in self.seen_request_ids:
                        continue
                    self.seen_request_ids.add(req_id)

                    try:
                        body = request.response.body
                        if body:
                            data = json.loads(body.decode('utf-8'))
                            if self.archive is not None:
                                self.archive.append(url, data)
                            self._process_api_response(url, data)
                    except Exception as e:
                        self.logger.debug(f"Error processing {url}: {e}")

    def _is_instagram_api(self, url: str) -> bool:
        return any(re.search(pattern, url) for pattern in self.api_patterns)

    def _process_api_response(self, url: str, data: Dict):
        if '/feed/timeline' in url or '/feed/user' in url:
            self._extract_timeline_posts(data)
        elif '/graphql' in url:
            self._extract_graphql_posts(data)
        elif '/media/' in url and 'info' in url:
            self._extract_media_info(data)

    def _extract_timeline_posts(self, data: Dict):
        try:
            items = (data.get('feed_items') or
                    data.get('items') or
                    data.get('data', {}).get('user', {}).get('edge_owner_to_timeline_media', {}).get('edges', []))

            if not items:
                return

            for item in items:
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

        except Exception as e:
            self.logger.error(f"Error extracting timeline: {e}")

    def _extract_graphql_posts(self, data: Dict):
        try:
            if 'xdt_api__v1__feed__timeline__connection' in data.get('data', {}):
                feed_connection = data['data']['xdt_api__v1__feed__timeline__connection']
                edges = feed_connection.get('edges', [])

                for edge in edges:
                    node = edge.get('node', {})
                    media = node.get('media')

                    if not media:
                        explore = node.get('explore_story') or {}
                        media = explore.get('media')

                    if not media:
                        continue

                    post_data = self._parse_media_object(media)
                    if post_data:
                        post_link = post_data.get('postLink')
                        if post_link and post_link not in self.seen_post_links:
                            self.seen_post_links.add(post_link)
                            self.captured_posts.append(post_data)
                return

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
            self.logger.error(f"Error extracting GraphQL: {e}")

    def _extract_media_info(self, data: Dict):
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
            self.logger.error(f"Error extracting media: {e}")

    def _parse_media_object(self, media: Dict) -> Optional[Dict]:
        try:
            inventory_source = media.get('inventory_source', '')
            user = media.get('user', {})
            friendship_status = user.get('friendship_status', {})
            is_following = friendship_status.get('following', False)
            is_suggested = (
                inventory_source in ['mixed_unconnected', 'explore_unconnected', 'suggested_post']
                or not friendship_status.get('following', True)
            )

            post_data = {
                'pk': media.get('pk') or media.get('id', ''),
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
                'is_suggested': is_suggested,
                'is_following': is_following,
            }

            if not post_data['postLink']:
                return None

            post_data['content_type'] = {
                'is_suggested': is_suggested,
                'from_followed_account': not is_suggested,
                'follow_action_taken': None,
                'follow_timestamp': None
            }

            return post_data

        except Exception as e:
            self.logger.debug(f"Error parsing media: {e}")
            return None

    def _get_post_link(self, media: Dict) -> str:
        code = media.get('code') or media.get('shortcode')
        username = self._get_username(media)
        if code and username:
            is_reel = media.get('product_type') == 'clips'
            path = 'reel' if is_reel else 'p'
            return f"https://www.instagram.com/{username}/{path}/{code}/"

        pk = media.get('pk') or media.get('id')
        if pk:
            return f"https://www.instagram.com/p/{pk}/"

        return ""

    def _get_timestamp(self, media: Dict) -> Optional[str]:
        taken_at = media.get('taken_at') or media.get('taken_at_timestamp')
        if taken_at:
            return str(taken_at)
        return None

    def _get_username(self, media: Dict) -> str:
        user = media.get('user', {})
        return user.get('username', '') or user.get('name', '')

    def _get_profile_url(self, media: Dict) -> str:
        username = self._get_username(media)
        if username:
            return f"https://www.instagram.com/{username}/"
        return ""

    def _is_verified(self, media: Dict) -> bool:
        user = media.get('user', {})
        return user.get('is_verified', False)

    def _get_like_count(self, media: Dict) -> int:
        """Raw integer like count. Returns 0 if unavailable."""
        return (media.get('like_count')
                or media.get('edge_liked_by', {}).get('count')
                or media.get('edge_media_preview_like', {}).get('count')
                or 0)

    def _get_caption(self, media: Dict) -> str:
        caption = media.get('caption')

        if caption:
            if isinstance(caption, dict):
                return caption.get('text', '')
            elif isinstance(caption, str):
                return caption

        edge_caption = media.get('edge_media_to_caption', {}).get('edges', [])
        if edge_caption and len(edge_caption) > 0:
            return edge_caption[0].get('node', {}).get('text', '')

        return ""

    def get_posts(self) -> List[Dict]:
        return self.captured_posts

    def clear(self):
        self.captured_posts = []
        self.seen_post_links = set()
        self.seen_request_ids = set()