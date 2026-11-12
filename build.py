#!/usr/bin/env python3
"""Smoji 表情镜像站构建脚本。

读取 catalog.tsv，把对应的表情图片下载到输出目录，并生成一个静态首页。
产物可以直接部署到 GitHub Pages、对象存储或任意静态服务器。

用法：
    python build.py --out _site --base-url https://name.github.io/smoji-mirror/

只依赖 Python 标准库，不需要 pip 安装任何东西。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = "https://s3-cdn.zsh.moe/smoji/{pack}/{item}.webp"
USER_AGENT = "dream-emoji-mirror/1.0 (static emoji mirror)"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_BYTES = 4 * 1024 * 1024


def read_catalog(path: Path) -> list[dict]:
    """解析 catalog.tsv，返回 [{id, label, items:[{id,label}]}]。"""
    packs: list[dict] = []
    current: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip("\r")
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            pack_id, _, label = line[1:].partition("\t")
            pack_id = pack_id.strip()
            if not ID_RE.fullmatch(pack_id):
                raise ValueError(f"非法包 ID：{pack_id!r}")
            current = {"id": pack_id, "label": label.strip() or pack_id, "items": []}
            packs.append(current)
            continue
        if current is None:
            continue
        item_id, _, label = line.partition("\t")
        item_id = item_id.strip()
        if ID_RE.fullmatch(item_id):
            current["items"].append({"id": item_id, "label": label.strip()})
    if not packs:
        raise ValueError("catalog.tsv 里没有读到任何表情包")
    return packs


def download_one(pack: str, item: str, dest: Path, retries: int = 3) -> tuple[bool, str]:
    """下载单个表情；已存在且大小正常时直接复用。"""
    if dest.is_file() and dest.stat().st_size > 120:
        return True, "cached"
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = SOURCE.format(pack=pack, item=item)
    last = ""
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "image/webp,image/*;q=0.9,*/*;q=0.1"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read(MAX_BYTES)
            if len(body) < 120 or body[:4] != b"RIFF" or body[8:12] != b"WEBP":
                last = "返回内容不是 WebP"
                continue
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(dest)
            return True, "ok"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code == 404:
                break
        except Exception as exc:  # 网络、DNS、SSL、超时
            last = str(exc) or exc.__class__.__name__
        time.sleep(1 + attempt)
    return False, last


def human_size(num_bytes: int) -> str:
    return f"{num_bytes / 1048576:.1f}"


def build_page(packs: list[dict], out_dir: Path, base_url: str, catalog_version: str) -> str:
    template = (ROOT / "index.template.html").read_text(encoding="utf-8")
    cards = []
    total_bytes = 0
    for pack in packs:
        previews = []
        for item in pack["items"]:
            path = out_dir / pack["id"] / f"{item['id']}.webp"
            if not path.is_file():
                continue
            total_bytes += path.stat().st_size
            if len(previews) < 5:
                previews.append(item)
        thumbs = "".join(
            f'<img src="{pack["id"]}/{item["id"]}.webp" alt="{html.escape(item["label"] or pack["label"])}" loading="lazy">'
            for item in previews
        )
        cards.append(
            '<article class="pack">'
            f'<div class="thumbs">{thumbs}</div>'
            f'<div class="meta"><h3>{html.escape(pack["label"])}</h3><span>{len(pack["items"])} 个</span></div>'
            f'<code class="slug">{pack["id"]}/</code>'
            "</article>"
        )
    total_items = sum(len(pack["items"]) for pack in packs)
    replacements = {
        "{{BASE_URL}}": html.escape(base_url),
        "{{PACKS}}": "\n".join(cards),
        "{{TOTAL_PACKS}}": str(len(packs)),
        "{{TOTAL_ITEMS}}": f"{total_items:,}",
        "{{SIZE_MB}}": human_size(total_bytes),
        "{{GENERATED}}": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "{{CATALOG_VERSION}}": catalog_version,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Smoji 表情镜像站")
    parser.add_argument("--out", default="_site", help="输出目录，默认 _site")
    parser.add_argument("--base-url", default="/", help="镜像站的公开地址前缀，用于页面展示")
    parser.add_argument("--workers", type=int, default=8, help="并发下载数，默认 8")
    parser.add_argument("--limit", type=int, default=0, help="每个包最多下载几个，0 表示全部（调试用）")
    parser.add_argument("--packs", default="", help="只处理指定包，逗号分隔")
    parser.add_argument("--skip-download", action="store_true", help="只生成页面，不下载图片")
    parser.add_argument("--keep", action="store_true", help="保留输出目录里已有的其他文件")
    args = parser.parse_args()

    catalog_path = ROOT / "catalog.tsv"
    packs = read_catalog(catalog_path)
    if args.packs.strip():
        wanted = {name.strip() for name in args.packs.split(",") if name.strip()}
        packs = [pack for pack in packs if pack["id"] in wanted]
        if not packs:
            print(f"没有匹配的包：{args.packs}", file=sys.stderr)
            return 1

    catalog_version = hashlib.sha256(catalog_path.read_bytes()).hexdigest()[:12]
    out_dir = (ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    if out_dir == ROOT or out_dir in ROOT.parents:
        print(f"拒绝把输出目录设为项目根目录或其上级：{out_dir}", file=sys.stderr)
        return 1
    if out_dir.exists() and not args.keep:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    base_url = (args.base_url or "/").strip()
    if not base_url.endswith("/"):
        base_url += "/"

    total = sum(min(len(pack["items"]), args.limit) if args.limit else len(pack["items"]) for pack in packs)
    print(f"目录版本 {catalog_version}：{len(packs)} 套，共 {total} 个表情")
    print(f"输出目录：{out_dir}")

    missing: list[tuple[str, str, str]] = []
    downloaded = cached = 0
    ok_by_pack: dict[str, int] = {pack["id"]: 0 for pack in packs}
    started = time.time()

    for index, pack in enumerate(packs, 1):
        items = pack["items"][: args.limit] if args.limit else pack["items"]
        print(f"[{index}/{len(packs)}] {pack['label']}（{pack['id']}）{len(items)} 个")
        if args.skip_download:
            continue
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(download_one, pack["id"], item["id"], out_dir / pack["id"] / f"{item['id']}.webp"): item
                for item in items
            }
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                ok, note = future.result()
                done += 1
                if ok:
                    ok_by_pack[pack["id"]] += 1
                    if note == "cached":
                        cached += 1
                    else:
                        downloaded += 1
                else:
                    missing.append((pack["id"], item["id"], note))
                if done % 100 == 0 or done == len(items):
                    print(f"    {done}/{len(items)}")

    page = build_page(packs, out_dir, base_url, catalog_version)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    if not args.skip_download:
        manifest = {
            "version": 1,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base": base_url,
            "catalog_version": catalog_version,
            "packs": [{"id": pack["id"], "label": pack["label"], "count": len(pack["items"])} for pack in packs],
        }
        (out_dir / "mirror.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    elapsed = time.time() - started
    print(f"完成：新下载 {downloaded} 个，复用 {cached} 个，失败 {len(missing)} 个，用时 {elapsed:.1f}s")
    if missing:
        for pack_id, item_id, note in missing[:10]:
            print(f"  失败 {pack_id}/{item_id}.webp：{note}", file=sys.stderr)
        # 允许极少量坏图，但整包缺失或失败率过高时让 CI 明确失败。
        empty_packs = [pack["id"] for pack in packs if ok_by_pack.get(pack["id"], 0) == 0]
        limit = max(5, int(total * 0.02))
        if empty_packs or len(missing) > limit:
            print(f"失败过多（阈值 {limit}），已中止部署", file=sys.stderr)
            return 1
    print(f"页面：{out_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
