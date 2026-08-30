"""
menu_ui.py - Terminal UI 三層選單系統 (Smart Agent v2.0)
Windows 相容，支援方向鍵導航、品牌分群、規格分層
"""
import os
import sys
import msvcrt
import json
import re
import urllib.request

# ─── 品牌分群關鍵字 ────────────────────────────────────────────────────────────

BRAND_KEYWORDS = {
    "Google / DeepMind":   ["gemma4","gemma3n","gemma3","gemma2","gemma","medgemma","shieldgemma","codegemma","functiongemma","embeddinggemma","translategemma"],
    "Meta / LLaMA":        ["llama4","llama3","llama2","llama","codellama","llava"],
    "Mistral AI":          ["mistral","mixtral","magistral","mathstral","codestral","devstral","ministral","mistrallite"],
    "Alibaba / Qwen":      ["qwen","qwq","codeqwen"],
    "DeepSeek":            ["deepseek","deepcoder","deepscaler"],
    "Microsoft / Phi":     ["phi4","phi3","phi","dolphin-phi"],
    "OpenAI OSS":          ["gpt-oss"],
    "Nvidia / Nemotron":   ["nemotron"],
    "Moonshot / Kimi":     ["kimi"],
    "IBM / Granite":       ["granite"],
    "Cohere":              ["command","aya"],
    "xAI / Grok":          ["grok"],
    "Minimax":             ["minimax"],
    "GLM / Zhipu":         ["glm"],
    "01.ai / Yi":          ["yi"],
    "Falcon":              ["falcon"],
    "LFM":                 ["lfm"],
    "EXAONE":              ["exaone"],
    "OlMo":                ["olmo"],
}
OTHER_BRAND = "其他模型"

WIDTH = 74

# ─── 工具函式 ──────────────────────────────────────────────────────────────────

def _getch():
    ch = msvcrt.getwch()
    if ch in ('\x00', '\xe0'):
        ch2 = msvcrt.getwch()
        return {
            'H': 'UP', 'P': 'DOWN', 'K': 'LEFT', 'M': 'RIGHT',
            'G': 'HOME', 'O': 'END', 'I': 'PGUP', 'Q': 'PGDN'
        }.get(ch2, None)
    elif ch == '\r':   return 'ENTER'
    elif ch == '\x1b': return 'ESC'
    elif ch == '\x08': return 'BACKSPACE'
    return ch

def _cls():
    os.system('cls')

def _bytes_to_str(size_bytes):
    if not size_bytes or size_bytes == 0:
        return "雲端模型"
    gb = size_bytes / 1_073_741_824
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{size_bytes / 1_048_576:.0f} MB"

def _size_tier(size_bytes):
    if not size_bytes or size_bytes == 0:
        return "cloud"
    gb = size_bytes / 1_073_741_824
    if gb < 10:   return "light"
    elif gb < 70: return "medium"
    else:         return "heavy"

TIER_LABEL = {
    "cloud":  "☁  雲端模型 (免下載)",
    "light":  "⚡  輕量    < 10 GB",
    "medium": "⚖  中量  10 - 70 GB",
    "heavy":  "🔥  重量    > 70 GB",
}
TIER_ORDER = ["cloud", "light", "medium", "heavy"]

def _get_brand(name: str) -> str:
    low = name.lower()
    for brand, keywords in BRAND_KEYWORDS.items():
        for kw in keywords:
            if low.startswith(kw):
                return brand
    return OTHER_BRAND

# ─── 目錄抓取（兩層策略）─────────────────────────────────────────────────────

def fetch_family_tags(family: str) -> list:
    """抓取某個 model family 所有 tags 的容量資訊。"""
    try:
        url = f"https://ollama.com/library/{family}/tags"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartAgent/2.0", "Accept":"text/html"})
        with urllib.request.urlopen(req, timeout=6) as r:
            html = r.read().decode('utf-8', errors='replace')
        # 從 HTML 抓 tag 與大小 (修正為抓取冒號後的 tag)
        entries = re.findall(
            r'href="/library/[^"]+:([^"]+)"[^>]*>.*?'
            r'(\d+(?:\.\d+)?\s*(?:GB|MB))',
            html, re.DOTALL
        )
        
        # 使用 dict 確保不重複
        results_map = {}
        for tag, size_str in entries:
            size_bytes = _parse_size_str(size_str)
            model_name = f"{family}:{tag}"
            if model_name not in results_map:
                results_map[model_name] = {"name": model_name, "size_str": size_str.strip(), "size_bytes": size_bytes}
                
        results = list(results_map.values())
        if not results:
            results.append({"name": f"{family}:latest", "size_str": "未知大小", "size_bytes": 0})
        return results
    except Exception:
        return [{"name": f"{family}:latest", "size_str": "未知大小", "size_bytes": 0}]

def _parse_size_str(s: str) -> int:
    s = s.strip()
    try:
        if "GB" in s:
            return int(float(s.replace("GB","").strip()) * 1_073_741_824)
        if "MB" in s:
            return int(float(s.replace("MB","").strip()) * 1_048_576)
    except Exception:
        pass
    return 0

def fetch_all_families() -> list:
    """從 ollama.com/library 抓取所有 model family 名稱。"""
    try:
        req = urllib.request.Request(
            'https://ollama.com/library',
            headers={'User-Agent': 'SmartAgent/2.0', 'Accept': 'text/html'}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode('utf-8', errors='replace')
        names = re.findall(r'/library/([a-z0-9_\-\.]+)', html)
        return sorted(set(names))
    except Exception:
        return []

def fetch_featured_catalog() -> list:
    """快速抓取首頁精選模型（fallback）。"""
    try:
        req = urllib.request.Request(
            'https://ollama.com/api/tags',
            headers={"User-Agent": "SmartAgent/2.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        result = []
        for m in data.get("models", []):
            name = m.get("name", "")
            size_bytes = m.get("size", 0)
            result.append({
                "name": name,
                "size_str": _bytes_to_str(size_bytes),
                "size_bytes": size_bytes
            })
        return result
    except Exception:
        return []

# ─── 渲染函式 ──────────────────────────────────────────────────────────────────

def _header(breadcrumbs: list, active: int):
    _cls()
    crumb_parts = []
    for i, b in enumerate(breadcrumbs):
        if i == active:
            crumb_parts.append(f"[{b}]")
        else:
            crumb_parts.append(b)
    crumb = "  ›  ".join(crumb_parts)
    print("+" + "═"*(WIDTH-2) + "+")
    print(f"║  Smart Agent 2.0  ─  {crumb:<{WIDTH-24}}║")
    print("+" + "═"*(WIDTH-2) + "+")

def _draw_list(items, selected, label_fn, subtitle="", page_size=16):
    total = len(items)
    if total == 0:
        print("  (無項目)")
        return
    start = max(0, min(selected - page_size // 2, total - page_size))
    start = max(0, start)
    end = min(total, start + page_size)

    if subtitle:
        print(f"  {subtitle}")
        print()

    for i in range(start, end):
        label = label_fn(items[i])
        if i == selected:
            line = f"  ▶  {label}"
            print(f"\033[97;44m{line:<{WIDTH}}\033[0m")
        elif isinstance(label, str) and label.startswith("──"):
            print(f"\033[90m  {label}\033[0m")
        else:
            print(f"     {label}")

    print()
    nav = []
    if start > 0:   nav.append(f"↑ 還有 {start} 項")
    if end < total: nav.append(f"↓ 還有 {total-end} 項")
    if nav: print("  " + "   ".join(nav))
    print("─" * WIDTH)
    print("  ↑↓ 選擇   →/Enter 確認/進入   ←/ESC 返回上層")

# ─── 第一層選單 ────────────────────────────────────────────────────────────────

def level1_menu(downloaded_models: dict, has_internet: bool,
                web_options: list, cloud_options: list, default_key: str = ""):
    items = []
    if has_internet:
        for key, cfg in web_options:
            items.append({"key": key, "cfg": cfg,
                          "label": f"🌐  [網頁版] {cfg['desc']}", "type": "web"})
        for key, cfg in cloud_options:
            items.append({"key": key, "cfg": cfg,
                          "label": f"☁   [雲端]   {cfg['desc']}  ─ 免下載", "type": "cloud"})

    if downloaded_models:
        for name, size in downloaded_models.items():
            items.append({"key": "__local__", "model_name": name,
                          "label": f"✓   [已下載] {name:<38} {size}", "type": "downloaded"})
    else:
        items.append({"key": None, "label": "  (尚無已下載的本地模型)", "type": "info"})

    items.append({"key": "__more__",
                  "label": "  ＋  更多模型...  瀏覽 Ollama 完整目錄  →", "type": "more"})
    items.append({"key": "__delete__",
                  "label": "  ✕  刪除本地模型  (ollama rm)", "type": "delete"})

    selected = 0
    if default_key:
        selected = next(
            (index for index, item in enumerate(items) if item.get("key") == default_key),
            selected,
        )
    crumbs = ["選擇模型", "Ollama 目錄", "品牌  ›  規格"]

    while True:
        _header(crumbs, 0)
        _draw_list(items, selected, lambda x: x["label"],
                   subtitle="已下載 / 常用模型  (→ 進入完整目錄)")
        key = _getch()
        if key == 'UP':
            selected = (selected - 1) % len(items)
        elif key == 'DOWN':
            selected = (selected + 1) % len(items)
        elif key == 'PGUP':
            selected = max(0, selected - 5)
        elif key == 'PGDN':
            selected = min(len(items)-1, selected + 5)
        elif key in ('ENTER', 'RIGHT'):
            item = items[selected]
            t = item["type"]
            if t == "info": continue
            if t in ("web", "cloud"):
                return item["key"], item["cfg"]["model"]
            if t == "downloaded":
                return "__local__", item["model_name"]
            if t in ("more", "delete"):
                return item["key"], None
        elif key in ('ESC', 'LEFT'):
            return None, None

# ─── 第二層選單 ────────────────────────────────────────────────────────────────

def level2_menu(families: list, downloaded_models: dict, cloud_supplement: list):
    """品牌分群選單。families = 所有 family 名稱的 list。"""
    from collections import defaultdict
    brand_map = defaultdict(list)
    for f in families:
        brand = _get_brand(f)
        brand_map[brand].append(f)

    brands = sorted(b for b in brand_map if b != OTHER_BRAND)
    if OTHER_BRAND in brand_map:
        brands.append(OTHER_BRAND)

    selected = 0
    crumbs = ["選擇模型", "Ollama 目錄 (品牌)", "品牌  ›  規格"]

    while True:
        _header(crumbs, 1)
        _draw_list(
            brands, selected,
            lambda b: f"{b:<32}  {len(brand_map[b]):>3} 款模型",
            subtitle="選擇品牌 / 公司  (→ 展開模型列表)"
        )
        key = _getch()
        if key == 'UP':
            selected = (selected - 1) % len(brands)
        elif key == 'DOWN':
            selected = (selected + 1) % len(brands)
        elif key == 'PGUP':
            selected = max(0, selected - 5)
        elif key == 'PGDN':
            selected = min(len(brands)-1, selected + 5)
        elif key in ('ENTER', 'RIGHT'):
            chosen_brand = brands[selected]
            result = level3_menu(brand_map[chosen_brand], chosen_brand,
                                 downloaded_models, cloud_supplement)
            if result is not None:
                return result
        elif key in ('ESC', 'LEFT'):
            return None

# ─── 第三層選單 ────────────────────────────────────────────────────────────────

def level3_menu(family_names: list, brand_name: str, downloaded_models: dict,
                cloud_supplement: list = None):
    """展開每個 family 的 tags，依容量分層列出。
    cloud_supplement: 來自 MODELS dict 的雲端模型 list，格式 [{name, size_str, size_bytes}, ...]
    """
    from collections import defaultdict

    _cls()
    print(f"  [*] 正在載入 {brand_name} 的模型清單... ({len(family_names)} 個系列)")

    # 先用 featured catalog 作為補充（已有容量資訊）
    # key = 完整 model 名稱 (e.g. "gpt-oss:120b-cloud", "gemma4:31b-cloud")
    featured_list = fetch_featured_catalog()

    # 加入來自 MODELS 字典的雲端模型（這些不在 Ollama API 裡）
    if cloud_supplement:
        for c in cloud_supplement:
            c_family = c["name"].split(":")[0].lower()
            if c_family in [f.lower() for f in family_names]:
                # 避免重複加入
                if not any(m["name"] == c["name"] for m in featured_list):
                    featured_list.append(c)

    all_models = []
    seen_names = set()

    for fam in family_names:
        # 找出 featured 中所有以該 family 為前綴的模型 (含 cloud 變體)
        fam_lower = fam.lower()
        matches = [
            m for m in featured_list
            if m["name"].lower().split(":")[0] == fam_lower
        ]
        if matches:
            for m in matches:
                if m["name"] not in seen_names:
                    all_models.append(m)
                    seen_names.add(m["name"])
        else:
            # 沒有精選資訊，放一個 :latest 佔位（大小未知）
            placeholder = f"{fam}:latest"
            if placeholder not in seen_names:
                all_models.append({
                    "name": placeholder,
                    "size_str": "未知大小",
                    "size_bytes": -1
                })
                seen_names.add(placeholder)

    # 以 size_bytes 分層
    tier_map = defaultdict(list)
    for m in all_models:
        sb = m.get("size_bytes", -1)
        if sb == 0:
            tier_map["cloud"].append(m)
        elif sb < 0:
            tier_map["light"].append(m)  # 未知容量放輕量
        else:
            tier_map[_size_tier(sb)].append(m)

    # 建立扁平清單
    items = []
    for tier_key in TIER_ORDER:
        if tier_key not in tier_map:
            continue
        items.append({"type": "header",
                      "label": f"── {TIER_LABEL[tier_key]} " + "─"*28})
        for m in sorted(tier_map[tier_key], key=lambda x: x.get("size_bytes", 0)):
            dl = " ✓" if (m["name"] in downloaded_models or
                          m["name"]+":latest" in downloaded_models) else ""
            items.append({
                "type": "model", "model": m,
                "label": f"  {m['name']:<42} {m['size_str']:>12}{dl}"
            })

    # 找第一個可選項
    selected = 0
    while selected < len(items) and items[selected]["type"] == "header":
        selected += 1
    if selected >= len(items): selected = 0

    crumbs = ["選擇模型", "Ollama 目錄 (品牌)", brand_name]

    while True:
        _header(crumbs, 2)
        _draw_list(items, selected, lambda x: x["label"],
                   subtitle=f"品牌：{brand_name}  (Enter 確認，← 返回品牌列表)")
        key = _getch()

        def skip_header(idx, direction=1):
            while 0 <= idx < len(items) and items[idx]["type"] == "header":
                idx += direction
            return idx

        if key == 'UP':
            selected = skip_header((selected - 1) % len(items), -1)
        elif key == 'DOWN':
            selected = skip_header((selected + 1) % len(items), 1)
        elif key == 'PGUP':
            selected = skip_header(max(0, selected - 5), -1)
        elif key == 'PGDN':
            selected = skip_header(min(len(items)-1, selected + 5), 1)
        elif key in ('ENTER', 'RIGHT'):
            if items[selected]["type"] == "model":
                chosen_model = items[selected]["model"]
                size_bytes = chosen_model.get("size_bytes", -1)
                
                if size_bytes == -1:
                    fam = chosen_model["name"].split(":")[0]
                    _cls()
                    print(f"\n  [*] 正在連線檢查 {fam} 可用的模型版本與容量...")
                    tags = fetch_family_tags(fam)
                    valid_tags = [t for t in tags if t["size_bytes"] > 0]
                    if not valid_tags:
                        print(f"\n  [!] 無法取得 {fam} 的下載資訊。")
                        print("      此模型可能僅提供雲端 API，或是尚未提供本地下載檔。")
                        input("\n  按 Enter 返回...")
                        continue
                        
                    result = level4_menu(brand_name, fam, valid_tags)
                    if result is not None:
                        return result
                    else:
                        continue
                else:
                    return chosen_model
        elif key in ('ESC', 'LEFT'):
            return None

# ─── 第四層選單 ────────────────────────────────────────────────────────────────

def level4_menu(brand_name: str, family: str, tags: list) -> dict:
    selected = 0
    crumbs = ["選擇模型", "Ollama 目錄 (品牌)", brand_name, family]

    while True:
        _header(crumbs, 3)
        _draw_list(tags, selected, lambda x: f"{x['name']:<42} {x['size_str']:>12}",
                   subtitle=f"選擇 {family} 的可用版本  (Enter 確認，← 返回上層)")
        key = _getch()

        if key == 'UP':
            selected = (selected - 1) % len(tags)
        elif key == 'DOWN':
            selected = (selected + 1) % len(tags)
        elif key == 'PGUP':
            selected = max(0, selected - 5)
        elif key == 'PGDN':
            selected = min(len(tags)-1, selected + 5)
        elif key in ('ENTER', 'RIGHT'):
            return tags[selected]
        elif key in ('ESC', 'LEFT'):
            return None

# ─── 入口 ──────────────────────────────────────────────────────────────────────

def run_model_selector(prompt_text: str, downloaded_models: dict,
                       has_internet: bool, web_options: list,
                       cloud_options: list, fetch_catalog_fn,
                       pull_model_fn, models_dict: dict, default_key: str = "") -> str | None:
    """
    三層選單流程。
    返回 model key (str in models_dict) 或 None。
    models_dict 直接傳入 smart_agent.MODELS，避免跨模組注入問題。
    """
    import subprocess

    while True:
        l1_key, l1_val = level1_menu(downloaded_models, has_internet,
                                      web_options, cloud_options, default_key=default_key)
        if l1_key is None:
            return None

        if l1_key == "__delete__":
            _manage_delete(downloaded_models)
            continue

        if l1_key in ("web_chatgpt", "web_gemini"):
            return l1_key

        if l1_key and l1_key.startswith("cloud_"):
            return l1_key

        if l1_key == "__local__":
            return _register_model(l1_val, None, models_dict)

        if l1_key == "__more__":
            _cls()
            print("[*] 正在從 Ollama 官方載入完整模型庫清單...")
            families = fetch_all_families()
            if not families:
                print("  [!] 無法載入，請確認網路連線。")
                input("  按 Enter 返回...")
                continue
            print(f"  [OK] 找到 {len(families)} 個模型系列。")

            # 從 models_dict 提取雲端模型作為補充資料
            cloud_supplement = [
                {"name": cfg["model"], "size_str": "☁ 雲端 (免下載)", "size_bytes": 0}
                for cfg in models_dict.values()
                if cfg.get("type") == "cloud"
            ]

            chosen_model = level2_menu(families, downloaded_models, cloud_supplement)
            if chosen_model is None:
                continue

            m_name = chosen_model["name"]
            size_bytes = chosen_model.get("size_bytes", -1)
            is_cloud = (size_bytes == 0)

            if not is_cloud:
                already = (m_name in downloaded_models or
                           m_name+":latest" in downloaded_models)
                if not already:
                    _cls()
                    print(f"\n  選擇了：{m_name}  ({chosen_model['size_str']})")
                    c = input("  確定要現在下載嗎？(y/N): ").strip().lower()
                    if c == "y":
                        success = pull_model_fn(m_name)
                        if not success:
                            input("  按 Enter 返回...")
                            continue
                        downloaded_models.update(_refresh_downloaded())
                    else:
                        continue

            return _register_model(m_name, chosen_model.get("size_str"), models_dict)


def _register_model(model_name: str, size_str, models_dict: dict) -> str:
    """直接注入到傳入的 models_dict（即 smart_agent.MODELS 的引用）。"""
    dyn_key = f"dyn_{model_name.replace(':', '_').replace('.','_').replace('-','_')}"
    if dyn_key not in models_dict:
        models_dict[dyn_key] = {
            "provider": "ollama", "model": model_name, "tier": 3,
            "type": "local", "desc": model_name,
            "estimated_size": size_str or "未知"
        }
    return dyn_key


def _refresh_downloaded() -> dict:
    import subprocess
    result = {}
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5).stdout
        for line in out.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 3:
                name = parts[0]
                size = ""
                for i, p in enumerate(parts):
                    if p in ["GB", "MB", "KB", "B"]:
                        size = f"{parts[i-1]} {p}"
                        break
                result[name] = size or "未知大小"
    except Exception:
        pass
    return result


def _manage_delete(downloaded_models: dict):
    import subprocess
    _cls()
    print("[管理本地模型 - 刪除]\n")
    if not downloaded_models:
        print("  目前沒有已下載的本地模型。")
        input("  按 Enter 返回...")
        return
    models = list(downloaded_models.keys())
    for i, m in enumerate(models, 1):
        print(f"  [{i:>2}] {m}  ({downloaded_models[m]})")
    print("\n  [0] 取消並返回\n")
    choice = input("請選擇要刪除的模型編號: ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(models):
            m = models[idx]
            c = input(f"  確定要刪除 {m} 嗎？(y/N): ").strip().lower()
            if c == 'y':
                subprocess.run(["ollama", "rm", m])
                downloaded_models.clear()
                downloaded_models.update(_refresh_downloaded())
                print("  [OK] 刪除成功。")
                input("  按 Enter 繼續...")
    except ValueError:
        pass
