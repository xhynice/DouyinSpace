# DouyinSpace

抖音弹幕采集 + 评论采集的 HF Space 部署配置。

## 仓库结构

```
├── DouyinBarrage/
│   ├── config.yaml      # 弹幕采集配置
│   └── rooms.txt        # 监控房间列表
├── DouyinComment/
│   └── config.yaml      # 评论采集配置
├── app.py               # Web 面板
├── templates/
│   └── index.html       # 面板模板
├── entrypoint.sh        # 启动脚本
├── supervisord.conf     # 进程管理配置
├── daily_crawl.sh       # 每日评论采集脚本
└── nginx.conf           # Nginx 配置
```

## 功能

- **弹幕采集**: 实时采集直播间弹幕、礼物、点赞等
- **录制**: 直播录制（支持原画/蓝光/超清/高清/标清）
- **评论采集**: 定时采集用户作品评论和回复
- **Web 面板**: 监控房间状态、录制文件、弹幕数据、作品数据
- **文件管理**: dufs 文件浏览器

## 相关仓库

- [xhynice/DouyinBarrage](https://github.com/xhynice/DouyinBarrage) - 弹幕采集核心代码
- [xhynice/DouyinComment](https://github.com/xhynice/DouyinComment) - 评论采集代码
- [sunset139/Douyin](https://huggingface.co/spaces/sunset139/Douyin) - HF Space

## 环境变量

| 变量 | 说明 |
|------|------|
| `DOUYIN_COOKIE` | 抖音 Cookie |
| `DUFS_PASSWORD` | 文件管理器密码 |
| `HF_TOKEN` | Hugging Face Token |

## 日志

日志统一输出到 `/data/logs/`：

| 文件 | 说明 |
|------|------|
| `barrage_stdout.log` | 弹幕采集输出 |
| `barrage_stderr.log` | 弹幕采集错误 |
| `webapp_stdout.log` | 面板输出 |
| `daily_crawl_*.log` | 每日评论采集日志 |
| `supervisord.log` | 进程管理日志 |
