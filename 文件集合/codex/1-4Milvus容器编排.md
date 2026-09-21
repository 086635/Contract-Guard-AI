# 1-4 · Milvus 容器编排（T-A3）

> 所属模块：1 · RAG　公共规范见 `0.施工标准与定义归属.md`

> 🔵 **本任务定位（2026-09-21 修订）**：Milvus 编排属 **B 组 · 可扩展性支线（P2）**，
> 且**必须以「可选 profile」形式落地** —— 不得改变 `docker compose up` 的默认行为，
> 不得影响既有的 `qdrant` 单容器部署。生产/演示主库仍是 **Qdrant**（见 `1.RAG模块.md` §1.1.3）。

## ① 元信息

| 项 | 值 |
|---|---|
| 任务 ID | **T-A3** |
| 优先级 | **P2**（**B 组 · 可扩展性支线**） |
| 人日 | 0.5 |
| 前置依赖 | 无（可并行） |
| 影响面 | `docker/docker-compose.yml` |
| 是否触碰代码 | ❌ 纯编排 |

---

## ② 范围

### ✅ 做

- `docker/docker-compose.yml` 新增 `etcd` 与 `milvus` 两个服务
- 为 `milvus` 配健康检查，避免依赖服务的启动竞态
- **复用 compose 里已有的 `minio`**（Milvus 依赖对象存储）

### ❌ 不做

- ❌ **不删 `qdrant` 服务**（它是对照与回退路径，见 `1-2Milvus适配器.md` §②）
- ❌ 不做 Milvus 集群（standalone 足够；集群化不在本次范围）
- ❌ 不改 `api` / `worker` 之外的既有服务

---

## ③ 定义归属

| 本文件**定义** | 本文件**引用**（不重复定义） |
|---|---|
| `etcd` / `milvus` 两个 compose 服务定义 | `minio` 服务（已存在于 compose） |
| Milvus 健康检查端点 | `CG_RAG_MILVUS_*` 配置项（`rag_core/config.py`，T-A2 定义） |

---

## ④ 交付物

| 文件 | 动作 | 职责 |
|---|---|---|
| `docker/docker-compose.yml` | 修改 | 新增 `etcd` + `milvus` 服务与 `etcddata` 卷 |

---

## ⑤ 接口规格 · 服务定义

追加到 `docker/docker-compose.yml` 的 `services:` 下（建议紧邻 `qdrant`，便于对照阅读），
并在文件末尾 `volumes:` 段补 `etcddata`。

```yaml
  # ---------------------------------------------------------------------------
  # Milvus standalone（T-A3 新增）
  #   ⚠️ 依赖 etcd（元数据）+ minio（对象存储，复用下方已有服务）
  #   ⚠️ 与 Qdrant 并存：Milvus 为生产/演示向量库，Qdrant 保留为对照与回退
  #   起服务：docker compose -f docker/docker-compose.yml up -d etcd milvus
  # ---------------------------------------------------------------------------
  etcd:
    image: quay.io/coreos/etcd:v3.5.16
    environment:
      - ETCD_AUTO_COMPACTION_MODE=revision
      - ETCD_AUTO_COMPACTION_RETENTION=1000
      - ETCD_QUOTA_BACKEND_BYTES=4294967296
      - ETCD_SNAPSHOT_COUNT=50000
    command: >
      etcd -advertise-client-urls=http://etcd:2379
      -listen-client-urls http://0.0.0.0:2379
      --data-dir /etcd
    volumes: ["etcddata:/etcd"]
    healthcheck:
      test: ["CMD", "etcdctl", "endpoint", "health"]
      interval: 10s
      timeout: 5s
      retries: 10

  milvus:
    image: milvusdb/milvus:v2.5.4          # ⚠️ 用具体小版本，勿用 latest
    command: ["milvus", "run", "standalone"]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000            # 复用 compose 已有的 minio 服务
      MINIO_ACCESS_KEY_ID: minioadmin
      MINIO_SECRET_ACCESS_KEY: minioadmin
    ports:
      - "19530:19530"                      # gRPC 业务端口（pymilvus 连接用）
      - "9091:9091"                        # 健康检查 / metrics
    volumes: ["milvusdata:/var/lib/milvus"]
    depends_on:
      etcd: { condition: service_healthy }
      minio: { condition: service_started }
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
      interval: 10s
      timeout: 5s
      retries: 20
      start_period: 60s                    # Milvus 启动较慢，给足冷启动时间
```

`volumes:` 段追加：

```yaml
  etcddata:
  milvusdata:
```

---

## ⑥ 实现规范 · 启动顺序与竞态（**本任务最容易被忽略的点**）

| 问题 | 说明 | 对策 |
|---|---|---|
| **Milvus 启动慢** | 冷启动通常 **30~60 秒**（etcd 就绪 → Milvus 加载 → 接受连接） | 健康检查加 `start_period: 60s` 与 `retries: 20` |
| **`api` / `worker` 抢跑** | 既有 compose 里 `api` / `worker` 对 qdrant 用的是 `condition: service_started`。若给 milvus 也这么写，应用会在 Milvus 未就绪时启动 | 若把 `CG_RAG_VECTOR_STORE` 切到 milvus，**必须**改成 `condition: service_healthy` |
| **首次检索静默降级** | 即便应用启动成功，Milvus 未 load 完成时首次 `search` 会失败 → `RAGService` **静默降级**（返回 `degraded=True`），看起来像「RAG 坏了」 | ① 用健康检查兜住；② 排查时**先看 `fallback_reason`**，若是 `STORE_UNAVAILABLE` 即为此因 |
| **端口冲突** | `9091` 若已被占用，健康检查会失败 | 检查 `netstat -ano \| findstr 9091` |

> ⚠️ **不建议**把自己的 `api` / `worker` 服务改成 `depends_on: milvus` 就完事 ——
> 开发时你可能只想起 Milvus 而让 api 走 inmemory。**更稳的做法是应用层容忍启动竞态**：
> `RAGService` 已有降级，配合健康检查 + 重试即可，不强制硬依赖。

---

## ⑦ Windows 约束（**必读**）

| 约束 | 说明 |
|---|---|
| 🔴 **Milvus Lite 不支持 Windows** | `MilvusClient("./milvus.db")` 的官方支持矩阵以 **Linux / macOS** 为主。Windows 上用 `mode="lite"` 大概率失败 |
| ✅ **Docker standalone 可用** | 这是 Windows 上的**唯一推荐路径** |
| 🟡 WSL2 内跑 Lite | 可行但跨文件系统访问慢、与宿主 Python 解释器分离，**不推荐作为主路径** |
| ✅ **混合策略（实际最推荐）** | **开发/单测用 `InMemoryVectorStore`（零依赖，已存在）**；**集成测试与演示用 Docker Milvus** |

> 💡 混合策略同时满足两个约束：
> ①本仓「默认路径必须离线可跑」的既有约定；②你「以 Milvus 为核心的向量库」的目标。
> 二者不矛盾——**Milvus 是生产实现，InMemory 是测试实现**，这正是 Ports & Adapters 的价值。

---

## ⑧ 验收标准

```powershell
# 1) 起服务
docker compose -f docker/docker-compose.yml up -d etcd milvus

# 2) 健康检查（应返回健康状态）
curl http://localhost:9091/healthz

# 3) 连通性（需先 uv add --package rag-core pymilvus）
python -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://localhost:19530'); print(c.list_collections())"

# 4) Qdrant 与其余服务未被破坏
docker compose -f docker/docker-compose.yml ps
```

| # | 断言 |
|---|---|
| 1 | `curl http://localhost:9091/healthz` 返回健康（非 5xx、非连接拒绝） |
| 2 | `MilvusClient(...).list_collections()` 成功返回（可以是空列表） |
| 3 | `docker compose ps` 中 `qdrant` / `postgres` / `redis` / `minio` 仍为既有状态（**未被破坏**） |
| 4 | Milvus 重启后数据仍在（`milvusdata` 卷持久化生效） |

---

## ⑨ 测试要求

本任务为纯编排，**无单测**。它的"测试"就是 §⑧ 的验收命令。

> ⚠️ 但要在 `1-5集成测试与迁移.md` 的集成测试里，
> **用 `CG_RAG_TEST_MILVUS_URI` 指向本任务起的服务**，形成闭环。

---

## ⑩ 避坑清单

| # | 坑 | 后果 | 正确做法 |
|---|---|---|---|
| 1 | `image: milvusdb/milvus:latest` | 版本漂移，行为不可复现 | **钉死具体小版本**（如 `v2.5.4`） |
| 2 | 忘记 `etcd` 服务 | Milvus 起不来 | etcd 是 Milvus standalone 的**必需依赖** |
| 3 | 忘记给 minio 凭证 | Milvus 无法读写对象存储 | `MINIO_ADDRESS` + `MINIO_ACCESS_KEY_ID` + `MINIO_SECRET_ACCESS_KEY` 三项都要 |
| 4 | 删除既有 `qdrant` 服务 | 失去对照与回退 | **保留** |
| 5 | 用 `service_started` 依赖 milvus | 启动竞态 → 首次检索静默降级 | 用 `service_healthy` + `start_period` |
| 6 | 未挂 `milvusdata` 卷 | 容器重启数据全丢，集成测试无法复现 | **必须挂卷** |
| 7 | 认为"Milvus 起了就万事大吉" | 首次检索可能仍降级 | 排查时**先看 `fallback_reason`** |
| 8 | 在 Windows 上试 `mode="lite"` | 大概率失败且报错不直观 | 用 `server` 模式 |

