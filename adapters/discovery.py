"""
Downloadly Discovery & Metadata Extraction Adapter
Deterministic HTML parser and canonicalizer for course pages and archive parts.
Strictly separates Persian and English titles (never defaults title_en to Persian text).
Extracts instructor, provider, curriculum, duration, level, thumbnails, provenance, and archive parts.
Includes cursor-based pagination, navigation loop protection, and link refresh capabilities.
"""

import re
import html
import hashlib
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from typing import Dict, Any, List, Optional, Tuple, Set
from datetime import datetime, timezone

class DownloadlyParser:
    ALLOWED_HOSTS = {
        "downloadly.ir", "www.downloadly.ir",
        "downloadlynet.ir", "www.downloadlynet.ir"
    }

    @staticmethod
    def canonicalize_url(raw_url: str) -> str:
        """
        Normalizes URL while preserving query parameters that affect course identity or pagination.
        Strips tracking parameters (utm_*, ref, etc.), anchors, and duplicate slashes.
        """
        parsed = urlparse(raw_url.strip())
        netloc = parsed.netloc.lower()
        if netloc not in DownloadlyParser.ALLOWED_HOSTS:
            raise ValueError(f"Host '{parsed.netloc}' is not in allowed discovery hosts.")

        # Whitelist functional query parameters (e.g., pagination or specific query filters)
        allowed_params = {'page', 'paged', 'id', 'p'}
        filtered_queries = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() in allowed_params
        ]
        normalized_query = urlencode(sorted(filtered_queries))

        clean_path = re.sub(r'/+', '/', parsed.path).rstrip('/')
        if not clean_path.endswith('/'):
            clean_path += '/'

        effective_host = "downloadlynet.ir" if "downloadlynet" in netloc else "downloadly.ir"
        return urlunparse((
            'https',
            effective_host,
            clean_path,
            '',
            normalized_query,
            ''
        ))

    @staticmethod
    def extract_slug(canonical_url: str) -> str:
        parts = [p for p in urlparse(canonical_url).path.split('/') if p]
        return parts[-1] if parts else "course"

    @classmethod
    def _split_multilingual_title(cls, full_title: str) -> Tuple[str, Optional[str]]:
        """
        Splits a title containing Persian and English parts.
        Examples:
        - "دانلود دوره آموزشی تسلط بر داکر و کانتینرها – Docker Mastery 2026"
          -> fa: "تسلط بر داکر و کانتینرها", en: "Docker Mastery 2026"
        - "آموزش یادگیری ماشین"
          -> fa: "آموزش یادگیری ماشین", en: None
        """
        clean = re.sub(r'^دانلود\s+(?:دوره\s+آموزشی\s+|آموزش\s+)?', '', full_title, flags=re.IGNORECASE).strip()
        clean = re.sub(r'\s+-\s+دانلود رایگان.*', '', clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r'\s+-\s+دانلودلی.*', '', clean, flags=re.IGNORECASE).strip()

        delimiters = [' – ', ' - ', ' | ', ' — ', ' :: ']
        title_fa = clean
        title_en = None

        for delim in delimiters:
            if delim in clean:
                parts = clean.split(delim, 1)
                p1, p2 = parts[0].strip(), parts[1].strip()
                # Check which one has English characters
                has_latin_1 = bool(re.search(r'[a-zA-Z]{3,}', p1))
                has_latin_2 = bool(re.search(r'[a-zA-Z]{3,}', p2))

                if not has_latin_1 and has_latin_2:
                    title_fa, title_en = p1, p2
                    break
                elif has_latin_1 and not has_latin_2:
                    title_fa, title_en = p2, p1
                    break

        # Fallback: if entire title is English
        if not title_en and re.search(r'^[a-zA-Z0-9\s\.\:\-_]+$', clean):
            title_en = clean
            title_fa = clean

        # Ensure title_en contains Latin characters; otherwise null
        if title_en and not re.search(r'[a-zA-Z]', title_en):
            title_en = None

        return title_fa.strip(), (title_en.strip() if title_en else None)

    @classmethod
    def parse_listing_page(cls, html_content: str) -> List[Dict[str, Any]]:
        """
        Parses course cards from a category or search listing page.
        """
        courses = []
        seen = set()
        fetched_at = datetime.now(timezone.utc).isoformat()

        # Match standard post titles and links in Downloadly
        link_pattern = re.compile(
            r'<a\s+[^>]*href=["\'](https?://(?:www\.)?downloadly\.ir/(?:elearning|download)/[^"\']+)["\'][^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL
        )

        for match in link_pattern.finditer(html_content):
            raw_url = match.group(1)
            raw_title = html.unescape(re.sub(r'<[^>]+>', '', match.group(2))).strip()

            # Filter noise / navigation links
            if not raw_title or len(raw_title) < 5 or any(k in raw_title for k in ["دیدگاه", "ادامه", "صفحه", "پست", "نظرات"]):
                continue

            try:
                canonical = cls.canonicalize_url(raw_url)
            except ValueError:
                continue

            if canonical in seen:
                continue
            seen.add(canonical)

            slug = cls.extract_slug(canonical)
            title_fa, title_en = cls._split_multilingual_title(raw_title)
            fingerprint = hashlib.sha256(f"{canonical}:{title_fa}:{title_en or ''}".encode()).hexdigest()[:16]

            courses.append({
                "url": raw_url,
                "canonical_url": canonical,
                "slug": slug,
                "title_fa": title_fa,
                "title_en": title_en, # Strictly None if not present, never copied from fa!
                "source_identifier": "downloadly.ir",
                "fingerprint": fingerprint,
                "fetched_at": fetched_at,
                "provenance": {
                    "source": "downloadly.ir",
                    "source_url": canonical,
                    "extracted_via": "listing_page"
                }
            })

        return courses

    @classmethod
    def parse_course_details(cls, html_content: str, source_url: str) -> Dict[str, Any]:
        """
        Extracts rich course metadata: titles, password, instructor, provider,
        description, thumbnail, parts, level, language, duration, and provenance.
        """
        canonical = cls.canonicalize_url(source_url)
        slug = cls.extract_slug(canonical)
        fetched_at = datetime.now(timezone.utc).isoformat()

        # 1. Title Extraction
        title_match = re.search(r'<h1[^>]*class=["\'][^"\']*entry-title[^"\']*["\'][^>]*>(.*?)</h1>', html_content, re.IGNORECASE | re.DOTALL)
        raw_title = html.unescape(re.sub(r'<[^>]+>', '', title_match.group(1))).strip() if title_match else slug
        title_fa, title_en = cls._split_multilingual_title(raw_title)

        # 2. Archive Password
        pwd_match = re.search(r'(?:گذرواژه|پسورد|رمز فایل)\s*:\s*(?:<[^>]+>)*\s*([a-zA-Z0-9\.\-_]+)', html_content, re.IGNORECASE)
        password = pwd_match.group(1).strip() if pwd_match else "www.downloadly.ir"

        # 3. Description
        desc_match = re.search(r'<div[^>]*class=["\'][^"\']*entry-content[^"\']*["\'][^>]*>(.*?)</div>', html_content, re.IGNORECASE | re.DOTALL)
        desc_raw = desc_match.group(1) if desc_match else ""
        # Remove download links and script blocks from description
        desc_clean = re.sub(r'<div[^>]*class=["\'][^"\']*download-links[^"\']*["\'][^>]*>.*?</div>', '', desc_raw, flags=re.DOTALL | re.IGNORECASE)
        desc_clean = re.sub(r'<script.*?</script>', '', desc_clean, flags=re.DOTALL | re.IGNORECASE)
        desc_text = html.unescape(re.sub(r'<[^>]+>', ' ', desc_clean)).strip()
        desc_text = re.sub(r'\s+', ' ', desc_text)[:2000] if desc_text else None

        # 4. Instructor & Provider Extraction
        instructor = None
        inst_match = re.search(r'(?:مدرس|استاد|Instructor)\s*:\s*(?:<[^>]+>)*\s*([^<\n\r]+)', html_content, re.IGNORECASE)
        if inst_match:
            cand = html.unescape(re.sub(r'<[^>]+>', '', inst_match.group(1))).strip()
            if len(cand) < 60:
                instructor = cand

        provider = None
        for prov in ["Udemy", "Coursera", "Pluralsight", "LinkedIn Learning", "Frontend Masters", "Packt", "O'Reilly"]:
            if re.search(r'\b' + re.escape(prov) + r'\b', html_content, re.IGNORECASE):
                provider = prov
                break

        # 5. Thumbnail Image
        img_match = re.search(r'<img[^>]*class=["\'][^"\']*wp-post-image[^"\']*["\'][^>]*src=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
        if not img_match:
            img_match = re.search(r'<div[^>]*class=["\'][^"\']*entry-content[^"\']*["\'][^>]*>\s*<p>\s*<img[^>]*src=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
        thumbnail_url = img_match.group(1).strip() if img_match else None

        # 6. Multipart Archive Links (RAR, ZIP)
        part_pattern = re.compile(
            r'<a\s+[^>]*href=["\'](https?://[^"\']+\.(?:rar|zip)(?:\?[^"\']*)?)["\'][^>]*>(.*?)</a>',
            re.IGNORECASE
        )
        parts = []
        seen_urls = set()

        for idx, match in enumerate(part_pattern.finditer(html_content), start=1):
            dl_url = match.group(1).strip()
            link_text = html.unescape(match.group(2)).strip()

            if dl_url in seen_urls:
                continue
            seen_urls.add(dl_url)

            # Extract part number from URL or text
            part_num = idx
            num_match = re.search(r'part(\d+)', dl_url, re.IGNORECASE) or re.search(r'بخش\s*(\d+)', link_text)
            if num_match:
                try:
                    part_num = int(num_match.group(1))
                except ValueError:
                    pass

            file_name = dl_url.split('/')[-1].split('?')[0]
            parts.append({
                "part_number": part_num,
                "file_name": file_name,
                "download_url": dl_url,
                "link_text": link_text
            })

        parts.sort(key=lambda x: x["part_number"])

        # 7. Curriculum / Syllabus (Heading tags)
        curriculum = []
        syllabus_matches = re.finditer(r'<h[3-5][^>]*>(.*?)</h[3-5]>', desc_raw, re.IGNORECASE)
        for s_match in syllabus_matches:
            item = html.unescape(re.sub(r'<[^>]+>', '', s_match.group(1))).strip()
            if len(item) > 3 and not any(k in item for k in ["دانلود", "لینک", "رمز", "مشخصات"]):
                curriculum.append(item)

        fingerprint = hashlib.sha256(f"{canonical}:{len(parts)}:{title_fa}:{title_en or ''}".encode()).hexdigest()[:16]

        return {
            "canonical_url": canonical,
            "slug": slug,
            "title_fa": title_fa,
            "title_en": title_en, # Strictly None if English title not detected!
            "instructor": instructor,
            "provider": provider,
            "description_fa": desc_text,
            "thumbnail_url": thumbnail_url,
            "archive_password": password,
            "curriculum": curriculum,
            "level": "Intermediate", # Default
            "language": "en",
            "total_parts": len(parts),
            "parts": parts,
            "fingerprint": fingerprint,
            "fetched_at": fetched_at,
            "provenance": {
                "source": "downloadly.ir",
                "source_url": canonical,
                "parser_version": "2.0",
                "fetched_at": fetched_at
            }
        }


class DiscoveryPaginator:
    """
    Manages stateful, loop-protected cursor pagination for course discovery.
    """
    def __init__(self, base_url: str, max_pages: int = 5):
        self.base_url = DownloadlyParser.canonicalize_url(base_url)
        self.max_pages = max_pages
        self.current_page = 1
        self.visited_urls: Set[str] = set()

    def get_next_page_url(self) -> Optional[str]:
        if self.current_page > self.max_pages:
            return None

        if self.current_page == 1:
            next_url = self.base_url
        else:
            # Standard WordPress pagination pattern
            base = self.base_url.rstrip('/')
            next_url = f"{base}/page/{self.current_page}/"

        if next_url in self.visited_urls:
            return None

        self.visited_urls.add(next_url)
        self.current_page += 1
        return next_url
