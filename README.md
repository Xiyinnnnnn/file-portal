# File Portal

超轻量密码保护的个人文件管理器，单文件无依赖，`python3 file_portal.py` 即用。

## 快速开始

```bash
# 运行（仅监听 127.0.0.1）
FILE_PORTAL_AUTH="user:password" python3 file_portal.py

# 指定端口 / 暴露目录
FILE_PORTAL_PORT=8080 FILE_PORTAL_ROOT=/srv/files FILE_PORTAL_AUTH="user:password" python3 file_portal.py

# 或用命令行参数（等价）
python3 file_portal.py --port 8080 --root /srv/files --auth "user:password"
```

浏览器打开 `http://127.0.0.1:8080` 输入账号密码即可浏览 / 上传 / 下载。

> **注意**：服务默认只绑定 127.0.0.1。需要外网访问时请自行加反向代理或隧道（如 cloudflared）。  
> **务必设置强密码**——隧道是真正的攻击面，本程序只是最后一道闸。

## 依赖

- Python 3.8+（仅标准库，零第三方依赖）
- 无 node、无 npm、无框架

## 项目架构

```
                        ┌─────────────────────────────┐
   Browser ── Basic ──▶ │  HTTP Basic Auth 校验        │
   (登录页 401 挑战)      │  user:pass ← FILE_PORTAL_AUTH│
                        └─────────────┬───────────────┘
                                      │ 通过
                    ┌─────────────────┴─────────────────┐
                    │       路由分发 (do_GET/do_POST)      │
                    └──┬──────────┬──────────┬──────────┘
                       │          │          │
                GET /  │  GET /files │   GET /file  │   POST /upload
               首页     │  目录浏览   │   文件下载    │   上传文件
                       ▼          ▼          ▼          ▼
              INDEX_PAGE    _resolve(rel)   _serve_file   _upload(rel)
                             │ 路径穿越防护   │              │
                             ▼              │              ▼
                    ROOT 内校验通过 ─────────┴──▶ 流式写出    multipart 解析
                    拒绝越界路径(404)                      落盘(重名自动加 _1)
```

路由一览：

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/` | 首页入口 |
| GET | `/files?path=REL` | 浏览 `ROOT/REL` 目录 |
| GET | `/file?path=REL` | 下载 `ROOT/REL` 文件（附件模式） |
| POST | `/upload?path=REL` | 上传文件到 `ROOT/REL` |

路径安全：所有 `REL` 经 `_resolve()` 解析后必须落在 `ROOT` 之内，`../` 越界一律 404。

## License

[MIT](LICENSE)
