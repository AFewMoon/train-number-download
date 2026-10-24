"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import export, formation, report, storage
from .config import Settings, load_settings
from .enrich import run_enrich
from .enumerate import enumerate_trains
from .http import HttpClient
from .probe import run_probe, write_probe_doc
from .stations import import_stations
from .timetable import crawl_timetable
from .util import setup_logger


def _client(settings: Settings, logger) -> HttpClient:
    return HttpClient(settings.http, settings.cache_dir, logger)


def _print_status(conn, logger) -> None:
    trains = conn.execute("SELECT COUNT(*) FROM trains").fetchone()[0]
    stops = conn.execute("SELECT COUNT(*) FROM train_stops").fetchone()[0]
    stations = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    formations = conn.execute("SELECT COUNT(*) FROM train_formation").fetchone()[0]
    logger.info("车次 %s 趟｜经停 %s 条｜车站 %s 个｜编组 %s 条", trains, stops, stations, formations)
    for row in conn.execute(
        "SELECT stage, status, COUNT(*) FROM crawl_tasks GROUP BY stage, status ORDER BY stage, status"
    ):
        logger.info("任务 %-10s %-8s %s", row[0], row[1], row[2])


def cmd_probe(settings: Settings, logger, args) -> int:
    with _client(settings, logger) as client:
        results = run_probe(settings, client, logger)
    path = write_probe_doc(settings, results)
    ok = sum(1 for check in results["checks"] if check["ok"])
    logger.info("体检完成：%s/%s 可用，报告 %s", ok, len(results["checks"]), path)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rail", description="中国铁路车次全量抓取工具")
    parser.add_argument("--config", help="配置文件路径（默认 config/settings.yaml）")
    parser.add_argument("--date", help="数据快照日期 YYYY-MM-DD（默认当天）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="接口体检并生成 docs/api-probe.md")
    sub.add_parser("stations", help="抓取车站库")
    sub.add_parser("status", help="查看当前数据与任务状态")

    p_trains = sub.add_parser("trains", help="枚举全量车次")
    p_trains.add_argument("--prefix", help="只枚举指定前缀，逗号分隔，如 G,D")
    p_trains.add_argument("--limit", type=int, help="最多枚举多少趟（小样本验证用）")
    p_trains.add_argument("--retry-failed", action="store_true", help="同时重跑失败的前缀")

    p_tt = sub.add_parser("timetable", help="抓取逐站时刻表")
    p_tt.add_argument("--limit", type=int, help="本次最多抓取多少趟")
    p_tt.add_argument("--retry-failed", action="store_true", help="同时重跑失败任务")

    p_enrich = sub.add_parser("enrich", help="补全车型/编组/里程")
    p_enrich.add_argument("--limit", type=int, help="本次最多处理多少趟")
    p_enrich.add_argument("--no-providers", action="store_true", help="跳过第三方，只用字典与规则")

    sub.add_parser("export", help="导出 CSV 与嵌套 JSONL")
    sub.add_parser("report", help="生成质量报告与失败清单")

    p_all = sub.add_parser("all", help="依次执行 stations -> trains -> timetable -> enrich -> export -> report")
    p_all.add_argument("--prefix", help="只枚举指定前缀，逗号分隔")
    p_all.add_argument("--limit", type=int, help="每个阶段最多处理多少趟（小样本验证用）")
    p_all.add_argument("--retry-failed", action="store_true", help="时刻表阶段同时重跑失败任务")
    p_all.add_argument("--no-providers", action="store_true", help="跳过第三方补全")

    args = parser.parse_args(argv)
    settings = load_settings(Path(args.config) if args.config else None, args.date)
    settings.ensure_dirs()
    logger = setup_logger(settings.log_path, args.verbose)
    logger.info("命令=%s 日期=%s 数据库=%s", args.command, settings.date, settings.db_path)

    conn = storage.connect(settings.db_path)
    storage.init_db(conn)

    try:
        if args.command == "probe":
            return cmd_probe(settings, logger, args)

        if args.command == "status":
            _print_status(conn, logger)
            return 0

        if args.command == "stations":
            with _client(settings, logger) as client:
                import_stations(client, settings, conn, logger)
            return 0

        if args.command == "trains":
            roots = [x.strip() for x in args.prefix.split(",") if x.strip()] if args.prefix else None
            with _client(settings, logger) as client:
                stats = enumerate_trains(
                    client, settings, conn, roots=roots, limit=args.limit,
                    logger=logger, retry_failed=args.retry_failed,
                )
            logger.info("枚举统计：%s", stats)
            return 0

        if args.command == "timetable":
            with _client(settings, logger) as client:
                crawl_timetable(client, settings, conn, limit=args.limit, retry_failed=args.retry_failed, logger=logger)
            return 0

        if args.command == "enrich":
            if not args.no_providers:
                run_enrich(conn, settings, logger, limit=args.limit)
            formation.apply_formation(conn, settings, logger, limit=args.limit)
            return 0

        if args.command == "export":
            export.export_csvs(settings, conn, logger)
            export.export_jsonl(settings, conn, logger)
            return 0

        if args.command == "report":
            metrics = report.collect_metrics(conn, settings)
            path = report.write_report(settings, conn, metrics)
            failures = report.write_failures(settings, conn)
            report.print_summary(metrics, logger)
            logger.info("报告：%s｜失败清单：%s", path, failures)
            return 0

        if args.command == "all":
            with _client(settings, logger) as client:
                import_stations(client, settings, conn, logger)
                roots = [x.strip() for x in args.prefix.split(",") if x.strip()] if args.prefix else None
                enumerate_trains(
                    client, settings, conn, roots=roots, limit=args.limit,
                    logger=logger, retry_failed=args.retry_failed,
                )
                crawl_timetable(client, settings, conn, limit=args.limit, retry_failed=args.retry_failed, logger=logger)
            if not args.no_providers:
                run_enrich(conn, settings, logger, limit=args.limit)
            formation.apply_formation(conn, settings, logger, limit=args.limit)
            export.export_csvs(settings, conn, logger)
            export.export_jsonl(settings, conn, logger)
            metrics = report.collect_metrics(conn, settings)
            report.write_report(settings, conn, metrics)
            report.write_failures(settings, conn)
            report.print_summary(metrics, logger)
            return 0

        parser.print_help()
        return 2
    except KeyboardInterrupt:
        logger.warning("已中断，任务状态已保存，可使用相同命令继续（自动跳过已完成项）")
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
