#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

MiB = 1024 * 1024

SOURCE_CODE = "SOURCE_CODE"
TEXT_DATA_CONFIG = "TEXT_DATA_CONFIG"
IMAGE_MEDIA = "IMAGE_MEDIA"
DOCUMENT_PACKAGE = "DOCUMENT_PACKAGE"
ARCHIVE_PACKAGE = "ARCHIVE_PACKAGE"
NATIVE_LIBRARY_BINARY = "NATIVE_LIBRARY_BINARY"
MODEL_WEIGHT = "MODEL_WEIGHT"
GENERATED_CACHE_UNKNOWN = "GENERATED_CACHE_UNKNOWN"

INLINE_OR_CHUNK = "INLINE_OR_CHUNK"
ATTACHMENT_IF_RELEVANT = "ATTACHMENT_IF_RELEVANT"
MANIFEST_ONLY = "MANIFEST_ONLY"
SKIP_CONTENT = "SKIP_CONTENT"


@dataclass(frozen=True)
class ProjectArtifactPolicy:
    classification: str
    max_single_file_bytes: int
    sync_mode: str
    oversize_behavior: str
    priority: int


POLICIES = {
    # Large generated/vendor translation units (for example SIMD tables in
    # x265) are still source code and can be safely split across transport
    # bundles.  Keep the limit class-specific so this does not enlarge bundle,
    # attachment, file-count, binary, or project-wide safety ceilings.
    SOURCE_CODE: ProjectArtifactPolicy(SOURCE_CODE, 10 * MiB, INLINE_OR_CHUNK, "REJECT_CLASS_LIMIT", 100),
    TEXT_DATA_CONFIG: ProjectArtifactPolicy(TEXT_DATA_CONFIG, 4 * MiB, INLINE_OR_CHUNK, "REJECT_CLASS_LIMIT", 90),
    IMAGE_MEDIA: ProjectArtifactPolicy(IMAGE_MEDIA, 50 * MiB, ATTACHMENT_IF_RELEVANT, "MANIFEST_ONLY_OVERSIZE", 70),
    DOCUMENT_PACKAGE: ProjectArtifactPolicy(DOCUMENT_PACKAGE, 50 * MiB, MANIFEST_ONLY, "MANIFEST_ONLY_OVERSIZE", 60),
    ARCHIVE_PACKAGE: ProjectArtifactPolicy(ARCHIVE_PACKAGE, 100 * MiB, MANIFEST_ONLY, "MANIFEST_ONLY_OVERSIZE", 40),
    NATIVE_LIBRARY_BINARY: ProjectArtifactPolicy(NATIVE_LIBRARY_BINARY, 100 * MiB, MANIFEST_ONLY, "MANIFEST_ONLY_OVERSIZE", 50),
    MODEL_WEIGHT: ProjectArtifactPolicy(MODEL_WEIGHT, 500 * MiB, MANIFEST_ONLY, "MANIFEST_ONLY_OVERSIZE", 50),
    GENERATED_CACHE_UNKNOWN: ProjectArtifactPolicy(GENERATED_CACHE_UNKNOWN, 10 * MiB, SKIP_CONTENT, "SKIP_CONTENT", 10),
}

_SOURCE_EXTS = {
    '.c','.cc','.cpp','.cxx','.h','.hh','.hpp','.hxx','.java','.kt','.kts','.py',
    '.cs','.go','.rs','.js','.jsx','.ts','.tsx','.sh','.bash','.bat','.cmd','.ps1',
    '.cmake','.gradle','.groovy','.swift','.m','.mm','.scala','.rb','.php','.lua',
}
_TEXT_EXTS = {
    '.md','.txt','.json','.jsonl','.yaml','.yml','.toml','.ini','.cfg','.conf','.xml',
    '.csv','.tsv','.html','.htm','.css','.scss','.proto','.properties','.lock','.sql',
    '.log','.rst','.tex','.dockerfile',
}
_IMAGE_EXTS = {'.png','.jpg','.jpeg','.webp','.bmp','.gif','.tif','.tiff','.heic','.heif','.avif','.dng','.raw'}
_DOCUMENT_EXTS = {'.pdf','.doc','.docx','.ppt','.pptx','.xls','.xlsx','.odt','.ods','.odp'}
_ARCHIVE_EXTS = {'.zip','.7z','.tar','.gz','.tgz','.bz2','.xz','.rar'}
_NATIVE_EXTS = {'.dll','.so','.dylib','.a','.lib','.exe','.elf','.apk','.aar','.jar','.bin','.obj','.o','.pdb','.class'}
_MODEL_EXTS = {'.onnx','.tflite','.pt','.pth','.ckpt','.safetensors','.pb','.engine','.mlmodel','.mlpackage','.gguf','.binmodel'}
_GENERATED_EXTS = {'.pyc','.pyo','.cache','.tmp','.temp'}
_SOURCE_NAMES = {'CMakeLists.txt','Dockerfile','Makefile','GNUmakefile','Android.mk','Android.bp'}


def _looks_text(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:8192]
    except OSError:
        return False
    if b'\x00' in sample:
        return False
    try:
        sample.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False


def classify_project_file(path: str | Path) -> dict:
    p = Path(path)
    suffix = p.suffix.lower()
    name = p.name
    if suffix in _MODEL_EXTS:
        classification = MODEL_WEIGHT
    elif suffix in _IMAGE_EXTS:
        classification = IMAGE_MEDIA
    elif suffix in _DOCUMENT_EXTS:
        classification = DOCUMENT_PACKAGE
    elif suffix in _ARCHIVE_EXTS:
        classification = ARCHIVE_PACKAGE
    elif suffix in _GENERATED_EXTS:
        classification = GENERATED_CACHE_UNKNOWN
    elif suffix in _NATIVE_EXTS:
        classification = NATIVE_LIBRARY_BINARY
    elif suffix in _SOURCE_EXTS or name in _SOURCE_NAMES:
        classification = SOURCE_CODE
    elif suffix in _TEXT_EXTS:
        classification = TEXT_DATA_CONFIG
    else:
        classification = TEXT_DATA_CONFIG if _looks_text(p) else GENERATED_CACHE_UNKNOWN
    policy = POLICIES[classification]
    return asdict(policy)


def policy_for_record(root: str | Path, record: dict) -> ProjectArtifactPolicy:
    classification = str(record.get('artifact_class') or '')
    if classification in POLICIES:
        return POLICIES[classification]
    return POLICIES[classify_project_file(Path(root) / str(record['path']))['classification']]


def artifact_policy_manifest() -> dict:
    return {
        key: {
            'max_single_file_bytes': value.max_single_file_bytes,
            'sync_mode': value.sync_mode,
            'oversize_behavior': value.oversize_behavior,
            'priority': value.priority,
        }
        for key, value in POLICIES.items()
    }


__all__ = [
    'ProjectArtifactPolicy','POLICIES','SOURCE_CODE','TEXT_DATA_CONFIG','IMAGE_MEDIA',
    'DOCUMENT_PACKAGE','ARCHIVE_PACKAGE','NATIVE_LIBRARY_BINARY','MODEL_WEIGHT',
    'GENERATED_CACHE_UNKNOWN','INLINE_OR_CHUNK','ATTACHMENT_IF_RELEVANT','MANIFEST_ONLY',
    'SKIP_CONTENT','classify_project_file','policy_for_record','artifact_policy_manifest',
]
