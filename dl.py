import asyncio
import random
import sys
import re
import sqlite3
import io
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from source import XHS
from source.module import Settings, ROOT, VOLUME

PROJECT_ROOT = ROOT
TXT_FILE_PATH = PROJECT_ROOT / "1.txt"
FAILED_TXT_PATH = PROJECT_ROOT / "failed.txt"
EXPLORE_DB_PATH = VOLUME / "ExploreID.db"
RECORDS_DB_PATH = VOLUME / "DownloadRecords.db"


class Tee:
    """同时输出到控制台和日志文件；倒计时类的 \\r 刷新只写控制台，保持日志干净"""

    def __init__(self, console, log_file):
        self.console = console
        self.log_file = log_file

    def write(self, text):
        self.console.write(text)
        if "\r" in text and "\n" not in text:
            return
        self.log_file.write(text)

    def flush(self):
        self.console.flush()
        self.log_file.flush()


def init_records_db():
    """创建作者/日期记录库（独立于原作者的 ExploreID.db）"""
    with sqlite3.connect(str(RECORDS_DB_PATH)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS downloads (
            note_id TEXT PRIMARY KEY,
            author_id TEXT,
            author_name TEXT,
            post_date TEXT,
            download_time TEXT)"""
        )


def backfill_records_from_folder():
    """回填：扫描 Download 目录，把磁盘上已有但未记录的作品补进记录库（下载时间取文件时间戳）
    兼容两种文件命名：带作品ID的（…_作者_ID_N.ext）和不带的（…_作者_标题_N.ext，用文件名做伪ID去重）"""
    dl_dir = VOLUME / "Download"
    if not dl_dir.is_dir():
        return 0
    added = 0
    with sqlite3.connect(str(RECORDS_DB_PATH)) as conn:
        for d in dl_dir.iterdir():
            dm = re.match(r"([0-9a-f]{24})_(.+)", d.name)
            if not (dm and d.is_dir()):
                continue
            for f in d.rglob("*"):
                if not f.is_file():
                    continue
                pm = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
                im = re.search(r"_([0-9a-f]{24})(?:_\d+)?\.[a-z0-9]+$", f.name)
                # 去掉结尾的 _序号.扩展名，作为无ID文件的伪作品ID
                base = re.sub(r"(?:_\d+)?\.[a-z0-9]+$", "", f.name)
                note_id = im.group(1) if im else f"f:{base}"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO downloads VALUES (?,?,?,?,?)",
                    (
                        note_id,
                        dm.group(1),
                        dm.group(2),
                        pm.group(1) if pm else None,
                        datetime.fromtimestamp(f.stat().st_mtime).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    ),
                )
                added += cur.rowcount
    return added


def record_download(note_id, since):
    """下载成功后，找出本轮处理期间新落盘的文件，从所在文件夹记录作者、从文件名记录发布日期"""
    dl_dir = VOLUME / "Download"
    files = [
        f
        for f in dl_dir.rglob("*")
        if f.is_file() and f.stat().st_mtime >= since
    ]
    if not files:
        return None
    author_id = author_name = post_date = None
    dm = re.match(r"([0-9a-f]{24})_(.+)", files[0].parent.name)
    if dm:
        author_id, author_name = dm.group(1), dm.group(2)
    pm = re.match(r"(\d{4}-\d{2}-\d{2})", files[0].name)
    if pm:
        post_date = pm.group(1)
    with sqlite3.connect(str(RECORDS_DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO downloads VALUES (?,?,?,?,?)",
            (
                note_id,
                author_id,
                author_name,
                post_date,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    return author_name, post_date


def print_stats():
    """按频道统计：作品数、文件涉及最新发布日期、最近下载时间"""
    with sqlite3.connect(str(RECORDS_DB_PATH)) as conn:
        rows = conn.execute(
            """SELECT author_name, COUNT(*), MAX(post_date), MAX(download_time)
            FROM downloads GROUP BY author_id ORDER BY 2 DESC"""
        ).fetchall()
    print(f"{'频道':<16}{'作品数':>5}   最新作品发布   最近下载时间")
    for name, n, latest_post, latest_dl in rows:
        print(f"{(name or '?')[:14]:<16}{n:>5}   {latest_post or '?'}     {latest_dl or '?'}")
    print(f"\n共 {len(rows)} 个频道, {sum(r[1] for r in rows)} 个作品")


def extract_id_from_url(url):
    # 兼容 /explore/{id}、/item/{id}、/discovery/item/{id}、/user/profile/{uid}/{id} 格式
    match = re.search(r'(?:/(?:explore|item)|/user/profile/[a-f0-9]+)/([a-f0-9]+)', url)
    return match.group(1) if match else None


def read_downloaded_ids():
    if not EXPLORE_DB_PATH.exists():
        return set()
    conn = sqlite3.connect(str(EXPLORE_DB_PATH))
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT ID FROM explore_id")
        return {row[0] for row in cursor.fetchall()}
    finally:
        conn.close()


def read_links_from_1txt(path=None):
    path = path or TXT_FILE_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        lines = content.split('\n')
        links = []
        for line in lines:
            parts = line.split()
            for part in parts:
                if part.strip():
                    links.append(part.strip())
        return links
    except Exception as e:
        print(f"读取链接文件出错: {e}")
        return []


def save_failed_links(failed_links, path=FAILED_TXT_PATH):
    if not failed_links:
        return
    try:
        with open(path, 'w', encoding='utf-8') as f:
            for link in failed_links:
                f.write(link + '\n')
        print(f"\n已将 {len(failed_links)} 个失败链接保存到 {path}")
    except Exception as e:
        print(f"保存失败链接出错: {e}")


def parse_statistics(output):
    pattern = r'共处理 (\d+) 个作品，成功 (\d+) 个，失败 (\d+) 个，跳过 (\d+) 个'
    match = re.search(pattern, output)
    if match:
        return {
            'all': int(match.group(1)),
            'success': int(match.group(2)),
            'fail': int(match.group(3)),
            'skip': int(match.group(4))
        }
    return None


def count_downloaded_files(output):
    # 统计实际下载成功的文件数（匹配"文件 xxx 下载成功"行）
    return len(re.findall(r'文件 .+? 下载成功', output))


async def download_link(xhs, link):
    print(f"\n{'='*50}")
    print(f"正在处理链接: {link}")

    output_buffer = io.StringIO()

    try:
        with redirect_stdout(output_buffer):
            await xhs.extract_cli(link, download=True, index=None)

        output = output_buffer.getvalue()
        print(output)
        stats = parse_statistics(output)
        file_count = count_downloaded_files(output)

        if stats:
            if stats['fail'] > 0:
                print(f"处理失败: 失败 {stats['fail']} 个")
                return False, False, file_count
            elif stats['skip'] > 0:
                print(f"处理跳过: 跳过 {stats['skip']} 个")
                return False, True, file_count
            else:
                print(f"处理成功: 成功 {stats['success']} 个作品，下载 {file_count} 个文件")
                return True, False, file_count
        else:
            print(f"处理成功: 下载 {file_count} 个文件")
            return True, False, file_count
    except Exception as e:
        error_msg = str(e)
        print(f"处理出错: {error_msg}")

        if "'browser_cookie'" in error_msg:
            print("检测到配置错误（browser_cookie），将跳过等待时间继续处理下一个链接")
            return False, True, 0
        else:
            return False, False, 0


async def main(txt_file="1.txt"):
    txt_path = Path(txt_file)
    if not txt_path.is_absolute():
        txt_path = PROJECT_ROOT / txt_path
    # 默认 1.txt 的失败链接仍写入 failed.txt；其他文件对应 <名>_failed.txt
    if txt_file == "1.txt":
        failed_path = FAILED_TXT_PATH
    else:
        failed_path = txt_path.parent / f"{txt_path.stem}_failed.txt"

    print(f"{'='*60}")
    print(f"小红书随机间隔批量下载器 - 直接调用API")
    print(f"链接文件: {txt_path.name}")
    print(f"下载间隔: 随机 2-5 分钟，每 20 个链接长休息 10-20 分钟")
    print(f"{'='*60}")

    links = read_links_from_1txt(txt_path)
    if not links:
        print("\n错误：未找到有效链接或文件读取失败！")
        return

    init_records_db()
    added = backfill_records_from_folder()
    if added:
        print(f"\n已回填 {added} 条磁盘既有作品的作者/日期记录")

    print(f"\n成功读取到 {len(links)} 个链接")

    downloaded_ids = read_downloaded_ids()
    if downloaded_ids:
        before = len(links)
        filtered = []
        skipped_existing = 0
        for link in links:
            item_id = extract_id_from_url(link)
            if item_id and item_id in downloaded_ids:
                skipped_existing += 1
            else:
                filtered.append(link)
        links = filtered
        print(f"数据库中共有 {len(downloaded_ids)} 条下载记录")
        print(f"过滤前 {before} 个链接，过滤后 {len(links)} 个待下载")
        if skipped_existing > 0:
            print(f"已跳过 {skipped_existing} 个已下载的作品（零网络请求）")
    else:
        print("未找到下载记录数据库，将处理所有链接")
        print(f"（首次运行或数据库路径不存在: {EXPLORE_DB_PATH}）")

    if not links:
        print("\n所有链接均已下载，无需处理！")
        return

    settings = Settings(VOLUME)
    params = settings.run()
    xhs = XHS(**params)
    print(f"运行模式: {'Cookie 已配置（登录态）' if params.get('cookie') else '匿名模式（无 Cookie）'}")

    failed_links = []
    success_count = 0
    skip_count = 0
    total_files = 0

    async with xhs:
        for i, link in enumerate(links, 1):
            print(f"\n{'-'*60}")
            print(f"[{datetime.now():%m-%d %H:%M:%S}] 开始处理链接 {i}/{len(links)}")

            t0 = datetime.now().timestamp()
            success, skipped, file_count = await download_link(xhs, link)
            total_files += file_count

            if success:
                success_count += 1
                nid = extract_id_from_url(link)
                if nid:
                    rec = record_download(nid, t0)
                    if rec:
                        print(f"已记录: {rec[0]} {rec[1]}")
            elif skipped:
                skip_count += 1
            else:
                failed_links.append(link)

            # 累计进度：当前/总数 + 成功/跳过/失败 + 已下载文件数
            progress = (
                f"进度 {i}/{len(links)} | "
                f"成功 {success_count} 跳过 {skip_count} 失败 {len(failed_links)} | "
                f"已下载 {total_files} 文件"
            )

            if i < len(links):
                # 失败或跳过时直接处理下一个，不倒计时
                if skipped or not success:
                    print(f"\n{progress}")
                    print("直接处理下一个链接...\n")
                else:
                    if i % 20 == 0:
                        wait_time = random.randint(600, 1200)
                        print(f"\n{progress}")
                        print(f"已连续处理 {i} 个链接，长休息 {wait_time // 60} 分钟...")
                    else:
                        wait_time = random.randint(120, 300)
                        print(f"\n{progress}")
                        print(
                            f"随机等待 {wait_time // 60} 分 {wait_time % 60:02d} 秒后处理下一个链接..."
                        )
                    for remaining in range(wait_time, 0, -1):
                        sys.stdout.write(f"\r倒计时: {remaining}秒 / 共 {wait_time}秒")
                        sys.stdout.flush()
                        await asyncio.sleep(1)
                    print("\n")

    print(f"\n{'='*60}")
    print(f"所有链接处理完成！")
    print(f"作品: 成功 {success_count}, 跳过 {skip_count}, 失败 {len(failed_links)}")
    print(f"文件: 实际下载 {total_files} 个")
    print(f"{'='*60}")

    save_failed_links(failed_links, failed_path)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--stats" in args:
        init_records_db()
        backfill_records_from_folder()
        print_stats()
    else:
        txt_file = next((a for a in args if not a.startswith("-")), "1.txt")
        log_dir = VOLUME / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"dl_{datetime.now():%Y%m%d_%H%M%S}.log"
        with open(log_path, "w", encoding="utf-8") as log_file, redirect_stdout(
            Tee(sys.stdout, log_file)
        ):
            asyncio.run(main(txt_file))
            print(f"\n本次运行日志: {log_path}")
