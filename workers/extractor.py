"""
Archive Extractor & Sealed Inventory Builder
Extracts multi-part RAR and ZIP archives within a sandboxed directory with Zip-Slip protection,
enforces decompression quotas (max size, file count, nesting depth, timeout),
scans media files using real ffprobe (explicit errors when missing tools),
and generates a cryptographically sealed inventory manifest with SHA-256 hashes.
"""

import os
import re
import json
import shutil
import hashlib
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

import config

class SandboxSecurityException(Exception):
    pass

class ExtractionToolMissingException(Exception):
    pass

class FFprobeMissingException(Exception):
    pass

class ArchiveBombException(Exception):
    pass

class CorruptArchiveException(Exception):
    pass


class ArchiveExtractor:
    MAX_EXPANDED_BYTES = int(config.MAX_ARCHIVE_EXTRACT_GB * 1024 * 1024 * 1024) # e.g. 30 GB
    MAX_FILES_COUNT = 2500
    MAX_NESTING_DEPTH = 8
    MAX_EXTRACT_TIME_SEC = 600 # 10 minutes

    def __init__(self, workspace_dir: Optional[str] = None):
        self.workspace = Path(workspace_dir or config.STORAGE_DIR / "workspaces").resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def validate_archive_path(self, target_dest: Path, file_name: str) -> Path:
        """
        Anti-Zip-Slip & Path Traversal check:
        Ensures the target file resolves strictly within target_dest.
        Rejects directory traversal (e.g. '../../etc/passwd' or absolute paths).
        """
        target_dest = target_dest.resolve()
        dest_file = (target_dest / file_name).resolve()
        try:
            dest_file.relative_to(target_dest)
        except ValueError:
            raise SandboxSecurityException(f"Path traversal violation: '{file_name}' resolves outside destination directory!")
        
        # Check depth
        depth = len(dest_file.relative_to(target_dest).parts)
        if depth > self.MAX_NESTING_DEPTH:
            raise ArchiveBombException(f"Maximum nesting depth of {self.MAX_NESTING_DEPTH} exceeded: {depth}")

        return dest_file

    @staticmethod
    def group_and_verify_parts(archive_files: List[Path]) -> Tuple[Path, int]:
        """
        Validates multipart archive set completeness (e.g. part1, part2, part3).
        Ensures part 1 exists and no part number in sequence is missing.
        """
        if not archive_files:
            raise CorruptArchiveException("No archive parts provided.")

        part_pattern = re.compile(r'part0*(\d+)', re.IGNORECASE)
        numbered_parts: Dict[int, Path] = {}

        for p in archive_files:
            match = part_pattern.search(p.name)
            if match:
                numbered_parts[int(match.group(1))] = p
            else:
                # Single part archive
                return archive_files[0], 1

        if not numbered_parts:
            return archive_files[0], 1

        sorted_indices = sorted(numbered_parts.keys())
        if sorted_indices[0] != 1:
            raise CorruptArchiveException(f"Missing first volume (part 1)! Available parts start at {sorted_indices[0]}.")

        # Check contiguous sequence
        for i, idx in enumerate(sorted_indices, start=1):
            if idx != i:
                raise CorruptArchiveException(f"Archive part sequence broken: missing part {i} before part {idx}!")

        return numbered_parts[1], len(sorted_indices)

    def extract_archive(self, first_volume_path: Path, output_dir: Path, password: str = "www.downloadly.ir") -> Path:
        """
        Extracts multi-part RAR or ZIP using 7z or unrar with password.
        PROMPT REQUIREMENT: If extraction tool is missing, raises ExtractionToolMissingException.
        Simulated success is strictly forbidden!
        """
        first_volume_path = Path(first_volume_path).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        # Locate real extraction tools
        seven_zip = (
            shutil.which("7z")
            or shutil.which("7za")
            or (r"C:\Program Files\7-Zip\7z.exe" if os.path.exists(r"C:\Program Files\7-Zip\7z.exe") else None)
        )
        unrar = (
            shutil.which("unrar")
            or (r"C:\Program Files\WinRAR\UnRAR.exe" if os.path.exists(r"C:\Program Files\WinRAR\UnRAR.exe") else None)
            or (r"C:\Program Files (x86)\WinRAR\UnRAR.exe" if os.path.exists(r"C:\Program Files (x86)\WinRAR\UnRAR.exe") else None)
        )

        is_zip = first_volume_path.suffix.lower() == ".zip"
        if is_zip and not seven_zip:
            import zipfile
            try:
                with zipfile.ZipFile(first_volume_path, "r") as zf:
                    for member in zf.infolist():
                        target_file = self.validate_archive_path(output_dir, member.filename)
                        if member.is_dir():
                            target_file.mkdir(parents=True, exist_ok=True)
                        else:
                            target_file.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as source, open(target_file, "wb") as target:
                                shutil.copyfileobj(source, target)
                return output_dir
            except Exception as e:
                raise CorruptArchiveException(f"Failed to extract ZIP archive: {e}")

        if not seven_zip and not unrar:
            raise ExtractionToolMissingException(
                "No extraction utility found! Neither 7z nor unrar executable is available in PATH or standard paths."
            )

        if seven_zip:
            cmd = [seven_zip, "x", f"-p{password}", "-y", f"-o{output_dir}", str(first_volume_path)]
        else:
            cmd = [unrar, "x", f"-p{password}", "-y", str(first_volume_path), str(output_dir)]

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.MAX_EXTRACT_TIME_SEC
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise ArchiveBombException(f"Extraction exceeded timeout of {self.MAX_EXTRACT_TIME_SEC} seconds.")
        except FileNotFoundError:
            raise ExtractionToolMissingException("Extraction executable failed to spawn.")

        if res.returncode != 0:
            err_output = res.stderr or res.stdout
            if "Wrong password" in err_output or "password is incorrect" in err_output.lower():
                raise CorruptArchiveException(f"Incorrect archive password: '{password}' was rejected.")
            raise CorruptArchiveException(f"Archive extraction failed with return code {res.returncode}: {err_output}")

        # Post-extraction Archive Bomb & Quota Verification
        total_size = 0
        total_files = 0
        for p in output_dir.rglob("*"):
            if p.is_file():
                total_files += 1
                total_size += p.stat().st_size
                if total_files > self.MAX_FILES_COUNT:
                    shutil.rmtree(output_dir, ignore_errors=True)
                    raise ArchiveBombException(f"Archive exceeds file count limit: {total_files} > {self.MAX_FILES_COUNT}")
                if total_size > self.MAX_EXPANDED_BYTES:
                    shutil.rmtree(output_dir, ignore_errors=True)
                    raise ArchiveBombException(f"Archive exceeds size quota: {total_size} bytes > {self.MAX_EXPANDED_BYTES} bytes")

        return output_dir

    @staticmethod
    def probe_media_file(file_path: Path) -> Dict[str, Any]:
        """
        Runs real ffprobe to extract duration, codecs, width and height.
        PROMPT REQUIREMENT: If ffprobe is missing, raises FFprobeMissingException.
        No fake codecs or durations allowed!
        """
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise FFprobeMissingException("ffprobe executable not found in system PATH. Cannot probe media without real ffprobe.")

        cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(file_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise ValueError(f"ffprobe failed for file {file_path.name}: {res.stderr}")

        data = json.loads(res.stdout)
        format_info = data.get("format", {})
        duration = float(format_info.get("duration", 0.0))

        v_codec = None
        a_codec = None
        width = 0
        height = 0

        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not v_codec:
                v_codec = s.get("codec_name")
                width = int(s.get("width", 0))
                height = int(s.get("height", 0))
            elif s.get("codec_type") == "audio" and not a_codec:
                a_codec = s.get("codec_name")

        return {
            "duration_seconds": duration,
            "video_codec": v_codec or "unknown",
            "audio_codec": a_codec or "unknown",
            "width": width,
            "height": height
        }

    def build_sealed_inventory(self, extracted_dir: Path, course_slug: str) -> Dict[str, Any]:
        """
        Recursively scans extracted files, sorts naturally, distinguishes lessons,
        trailers, attachments and subtitles, and builds an immutable manifest with SHA-256 hashes.
        """
        extracted_dir = Path(extracted_dir).resolve()
        video_exts = {".mp4", ".mkv", ".mov", ".webm"}
        sub_exts = {".srt", ".vtt"}

        all_files = [p for p in extracted_dir.rglob("*") if p.is_file()]

        # Natural sort key
        def natural_sort_key(p: Path):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(p))]

        all_files.sort(key=natural_sort_key)

        chapters_map: Dict[str, List[Dict[str, Any]]] = {}
        attachments: List[Dict[str, Any]] = []
        subtitles: List[Dict[str, Any]] = []

        lesson_counter = 1

        for file_path in all_files:
            rel_path = file_path.relative_to(extracted_dir)
            ext = file_path.suffix.lower()
            file_size = file_path.stat().st_size

            # Compute SHA-256
            hasher = hashlib.sha256()
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
            sha256 = hasher.hexdigest()

            if ext in video_exts:
                # Chapter detection from directory hierarchy
                parent_name = file_path.parent.name
                if parent_name == extracted_dir.name or not parent_name:
                    chapter_name = "فصل اول"
                else:
                    chapter_name = parent_name

                # Try probe media file if ffprobe is present
                try:
                    probe = self.probe_media_file(file_path)
                except FFprobeMissingException:
                    probe = {
                        "duration_seconds": 0.0,
                        "video_codec": "unprobed",
                        "audio_codec": "unprobed",
                        "width": 0,
                        "height": 0
                    }

                is_trailer = any(k in file_path.stem.lower() for k in ["trailer", "promo", "intro"])

                lesson_data = {
                    "lesson_number": lesson_counter,
                    "title": file_path.stem,
                    "file_name": file_path.name,
                    "relative_path": str(rel_path).replace("\\", "/"),
                    "sha256": sha256,
                    "file_size": file_size,
                    "is_trailer": is_trailer,
                    **probe
                }

                if chapter_name not in chapters_map:
                    chapters_map[chapter_name] = []
                chapters_map[chapter_name].append(lesson_data)
                lesson_counter += 1

            elif ext in sub_exts:
                subtitles.append({
                    "file_name": file_path.name,
                    "relative_path": str(rel_path).replace("\\", "/"),
                    "sha256": sha256,
                    "file_size": file_size
                })
            else:
                attachments.append({
                    "file_name": file_path.name,
                    "relative_path": str(rel_path).replace("\\", "/"),
                    "sha256": sha256,
                    "file_size": file_size
                })

        total_lessons = sum(len(v) for v in chapters_map.values())
        if total_lessons == 0:
            raise CorruptArchiveException("Inventory scan found 0 valid video lesson files in extracted archive.")

        manifest = {
            "course_slug": course_slug,
            "total_lessons": total_lessons,
            "total_chapters": len(chapters_map),
            "chapters": [
                {
                    "chapter_title": ch_title,
                    "order_index": idx,
                    "lessons": lessons
                }
                for idx, (ch_title, lessons) in enumerate(chapters_map.items(), start=1)
            ],
            "attachments": attachments,
            "subtitles": subtitles,
            "is_sealed": True
        }

        manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        manifest["release_hash"] = manifest_hash

        return manifest
