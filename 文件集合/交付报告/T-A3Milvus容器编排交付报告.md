# T-A3 · Milvus 容器编排 交付报告（排期项 `0-C2`）

> 交付日期：**2026-09-23**　范围：`文件集合/codex/0-C2.md`（权威规格：`文件集合/codex/1-4Milvus容器编排.md` §⑤~§⑧）
> 分支：`codex/impl-rag-agent-ocr`
> 提交：`7d012a9`（本卡实现）　附属修复：`6c90844`（`fix(T-A2)`）、`4bc8f8f`（`fix(T-A1)`）
> 本报告是**人看的交付说明**，不是实现依据 —— 实现依据一律以 `文件集合/codex/**`（L0）为准。
> 反过来，本报告里的**实测数字与偏差登记**应当反过来修正文档（本轮已改的都在 §4 / §5 列出）。

---

## 0. 一页速览

| 交付项 | 状态 | 关键证据 |
|---|---|---|
| **编排**：`docker/docker-compose.yml` 新增 `etcd` + `milvus`（+ 两个卷） | ✅ | `docker compose config --quiet` → OK；`docker compose ps` → etcd / milvus / minio 三服务 Up |
| **断言 1**：健康检查 | ✅ | `Invoke-WebRequest http://localhost:9091/healthz` → **HTTP 200，body=`OK`** |
| **断言 2**：连通性 | ✅ | `MilvusClient(uri='http://localhost:19530').list_collections()` → **`['t_a3_smoke']`** |
| **断言 3**：既有服务未被破坏 | ✅ | 本项目仅 etcd/milvus/minio；`qdrant`/`postgres`/`redis` 本卡未起、状态未变；宿主 **Dify 栈 11 个容器全程 Up** |
| **断言 4**：重启后数据仍在 | ✅ | `restart milvus` → **10 s 后 healthy**；重启后 `t_a3_smoke` 与 `T-A3-DOC` 切片均可查回 |
| **🔴 高危坑修复**：compose 项目名隔离 | ✅ | 显式 `name: contract-guard`；实测 `docker compose ls` = `contract-guard running(3)` + `docker running(11)` 两条独立项目 |
| **网络纠偏**：MinIO 镜像改 Quay | ✅ | `quay.io/minio/minio:latest` 直连可拉（Docker Hub 经 CN 加速器 `denied`） |
| **附属修复 A**（`fix(T-A2)`） | ✅ | 无服务断言改用确定性关闭端口，摆脱对「19530 恰好没人监听」的依赖 |
| **附属修复 B**（`fix(T-A1)`） | ✅ | `count()` 改 `count(*)`+`Strong`：真 server 上 `0 → 3 → 3 → 0`（原实现给 `0 → 0 → 0 → 3`） |

**一句话**：Milvus **真的在本机跑起来了**（不是只写 compose）—— 两条容器的健康检查、连通性、
持久化四条断言全部在真机上通过；顺带排掉了两个会**误伤别人**的坑（项目名撞 Dify、Docker Hub 被加速器拒），
并暴露出一个 lite 上测不出的适配器缺陷（`count()`），已修并回填文档。

## 1. 本轮做了什么（顺序）

1. **前置核销**：确认 Docker daemon 可用（`docker version` → client/server **29.7.2**，Linux 容器）；
   `docker compose ls` 摸清宿主上已有两个项目（`docker` = Dify 11 容器、`multi-agent-rag-customer-support`）。
2. **摸镜**：逐个试拉 `milvusdb/milvus:v2.5.4` / `quay.io/coreos/etcd:v3.5.16` / `minio/minio:latest` →
   发现 MinIO 被加速器拒（见 §4.2），改 Quay。
3. **改 compose**：加 `etcd` / `milvus` 服务段 + 两个卷 + `name: contract-guard`（见 §4.1）。
4. **起服务**：`docker compose -f docker/docker-compose.yml up -d etcd minio milvus` → 三容器 Up，milvus 冷启动约 60 s 转 healthy。
5. **跑 §⑦ 四条断言**（逐条见 §3），其中断言 4 需先往集合里留一条数据。
6. **走本仓适配器做真机联调**（T-A1 适配器在真 server 上的首次验证）→ 发现 `count()` 口径缺陷（见 §5.2）。
7. **修两处连带问题**（`fix(T-A2)` / `fix(T-A1)`）→ 全量门禁回到 398 passed / 8 skipped / 0 failed。
8. **回填**：`0-C2.md`（§⑦ 回填表 + 新增 §⑩ 落地记录）、`1-4`（§⑧ 新增实测后回填）、
   `0-C1.md`（§⑩.5）、`1-2`（§⑪.3 第 7 行 + §⑪.6）、`1.C5`（§⑥ 追加表第 7 行 + §⑦ 纠偏块）。

## 2. 逐项交付

| 落点（`0-C2 §④`） | 改了什么 | 关键口径 |
|---|---|---|
| `docker/docker-compose.yml` | `name: contract-guard` | 见 §4.1。**没有这一行，`down` 会停掉 Dify 整栈** |
| 同上 | `etcd` 服务段 | `quay.io/coreos/etcd:v3.5.16`；挂 `etcddata`；`etcdctl endpoint health`；auto-compaction / quota 环境变量逐字对齐 `1-4 §⑤` |
| 同上 | `milvus` 服务段 | `milvusdb/milvus:v2.5.4`（**不用 latest**）；`command: ["milvus","run","standalone"]`；端口 `19530`/`9091`；复用 `minio`；挂 `milvusdata`；`depends_on` = etcd `service_healthy` + minio `service_started`；健康检查 `curl -f http://localhost:9091/healthz` + `start_period: 60s` + `retries: 20` |
| 同上 | `minio` 镜像 | `minio/minio:latest` → `quay.io/minio/minio:latest`（附注释说明原因，见 §4.2） |
| 同上 | `volumes:` | 补 `etcddata:` / `milvusdata:` |

**没有改的**（`0-C2 §②` 的「不做」逐条守住）：`qdrant` / `postgres` / `redis` / `minio` 的**服务定义**、
`api` / `worker`（未给它们加 `depends_on: milvus` 硬依赖）；未新增任何 Python 代码；
未触碰 `rag_contracts` / `service.py`。

## 3. 实测证据

### 3.1 卡内 `§⑦` 四条断言

| # | 断言 | 实测输出 |
|---|---|---|
| 1 | `/healthz` 健康 | `HTTP 200 body=OK` |
| 2 | `list_collections()` 成功返回 | `['t_a3_smoke']`（服务初起为空 `[]`） |
| 3 | 既有服务未被破坏 | `docker compose ps` → 仅 `etcd`/`milvus`/`minio`；宿主 `docker ps` → Dify **11 个容器全部 Up 8 minutes**，无一受影响 |
| 4 | 重启后数据仍在 | `docker compose restart milvus` → `healthy after 10s`；重启后 `t_a3_smoke` 仍在，`doc_id=T-A3-DOC`、`section_id=第2条` 的切片可查回 |

### 3.2 项目隔离（§4.1 的效果验证）

```text
$ docker compose ls
NAME                STATUS              CONFIG FILES
contract-guard      running(3)          E:\important_project\Serve_bage\docker\docker-compose.yml
docker              running(11)         D:\Dify\dify\docker\docker-compose.yaml

$ docker ps --format "{{.Names}}"
contract-guard-etcd-1     contract-guard-milvus-1    contract-guard-minio-1     <- 本项目
docker-nginx-1            docker-api-1               docker-web-1     ...      <- Dify，未被波及
```

### 3.3 走本仓适配器的真机联调（T-A1 适配器首次跑在真 server 上）

用 `MilvusVectorStore(uri="http://localhost:19530", collection="t_a3_smoke")` 实测：

- `health()` → `True`；`ensure_collection(1024)` 成功（真机建集合 + HNSW/COSINE 索引）
- `upsert()` 返回写入数；`search("逾期付款 违约金")` 能查到，`ContractChunk.from_payload(hits[0].payload)` **可还原**
- `delete_by_doc("T-A3-DOC")` 返回删除数
- ⚠️ `count()` **恒返回 0** → 展开为 §5.2 的对照实验

## 4. 两条纠偏（**新发现的高危坑，均已写进 compose 注释**）

### 4.1 🔴 compose 项目名必须显式固定（否则会停掉别人的栈）

- **现象**：`docker/docker-compose.yml` 位于 `docker/` 下，compose 默认用**文件所在目录名**作项目名，
  即默认项目名就是 `docker`。
- **实测**：本机另有一个同样放在 `docker/` 下的 compose 项目 —— Dify
  （`D:\Dify\dify\docker\docker-compose.yaml`），其默认项目名**恰好也是 `docker`**，
  `docker compose ls` 显示 `docker running(11)`。
- **后果**：两栈共用命名空间 → 本项目 `up` 会把 Dify 的 11 个容器当成 orphan 提示清理；
  `down` 更会**直接停掉 Dify 整栈**。
- **处置**：顶部加 `name: contract-guard`。实测生效（§3.2）。

### 4.2 MinIO 镜像改用 Quay 仓库（本机网络限制，非设计变更）

- **现象**：`minio/minio:latest` 拉取失败 → `error from registry: denied`。
- **实测**：本机 Docker 走 CN 加速器（`docker.m.daocloud.io` / `hub-mirror.c.163.com` /
  `mirror.baidubce.com`，由 `docker info` 的 `RegistryConfig` 读出），该加速器对 Docker Hub 的
  `minio/minio` 拒绝服务；`quay.io/minio/minio:latest` **直连可拉**。
- **处置**：改 `quay.io/minio/minio:latest`。**同一官方镜像、同一条 `server /data` 命令、
  同一组 `MINIO_*` 环境变量，行为不变**，故不违反「保留既有服务」。
  `milvusdb/milvus:v2.5.4` 与 `quay.io/coreos/etcd:v3.5.16` 均可正常拉取，无需改动。

## 5. 两处附属修复（本卡的**连带产物**，各自独立成笔）

### 5.1 `fix(T-A2)` `6c90844`：无服务断言不该依赖「19530 恰好没人监听」

起服务后全量门禁立刻出现 **1 failed**：`test_milvus_branch_raises_store_unavailable_without_server`
→ `Failed: DID NOT RAISE StoreUnavailableError`。

**根因**：该用例（`0-C1 §⑩.1` 的第 2 条替身）用配置默认 `milvus_uri=http://localhost:19530`，
把「宿主恰好没起服务」当成了断言前提；而本卡的交付物正是让 `19530` 上有服务。
原 `CG_RAG_TEST_MILVUS_URI` skip 守卫也救不了 —— 本卡**不设**该 env。

**处置**：新增 `_closed_port_uri()`（向 OS 借一个端口后立即释放，得到**确定无人监听**的端点），
被测 URI 由「默认值」改为该端点。`pytest.raises(StoreUnavailableError)` **一字未动**，
改的是**输入** —— 这不是"放宽断言"，而是把「依赖宿主恰好没起服务」变成「与宿主无关、恒成立」。
skip 守卫随之删除（断言已与环境无关，无再跳过的理由；覆盖面只增不减）。

**实测**：`pytest packages/rag-core/tests/test_config_factory.py` → **17 passed**（该条 6.76 s，含 `pymilvus` 真实 import + 一次连接失败）。

### 5.2 `fix(T-A1)` `4bc8f8f`：`MilvusVectorStore.count()` 在真 server 上口径错误

**对照实验**（真 server v2.5.4，同一集合，逐时点）：

| 时点 | `get_collection_stats()["row_count"]`（原实现） | `count(*)` + `consistency_level="Strong"` |
|---|---|---|
| 空集合 | 0 | 0 |
| `upsert` 3 条后**立刻** | **0** ❌ | **3** ✅ |
| `flush()` 后 | 3 | 3 |
| 同主键再 `upsert` 3 条后立刻 | **0** ❌ | **3** ✅（幂等，不增长） |
| `delete` 全部后立刻 | **0** ❌ | **0** ✅ |
| `delete` 后 `flush()` | **3** ❌（含**已逻辑删除**的行） | 0 ✅ |

**结论**：`row_count` 是**物理行数**（含逻辑删除）且**未 flush 前为 0** —— 两处都与
`QdrantVectorStore.count(exact=True)`（存活点数、即时一致）语义不符，
**不是可接受的版本差异**，且 milvus-lite 3.0 上**测不出**。

**处置**：`count()` 改用 `query(filter="", output_fields=["count(*)"], consistency_level="Strong")`。
**刻意不调 `flush()`**：它是重操作（落盘 + 封段），且会让「同主键重复写入 → `count()` 不增长」
这类断言依赖副作用。真机复验：`0 → 3 → 3 → 0`。

**为何必须现在修**：`0-C3`（T-A4）的断言 2「同一 `chunk_id` `upsert` 两次 → `count()` 仍为 1」
与断言 6「`delete_by_doc()` 后 `count()` 减少」**在真 server 上原本必然失败**，
而 `0-C3 §④` 明确「不改生产代码」→ 本修复是 `0-C3` 能全绿的前提。

## 6. 门禁读数

| 项 | 读数 |
|---|---|
| 全量 `pytest` | **398 passed / 8 skipped / 0 failed**（91.26 s；与 T-A2 基线一致，**含本卡两处附属修复后仍逐字持平**） |
| `docker compose config --quiet` | OK（exit 0） |
| ruff check（改动文件） | All checks passed |
| ruff format --check（改动文件） | 2 files already formatted |
| mypy（`milvus_store.py`） | Success: no issues found in 1 source file |
| 卡内断言 | §⑦ 四条 **全部通过**（§3.1） |

## 7. 未做项与下一步

**未做项（逐条）**

- ❌ `api` / `worker` 的 `depends_on: milvus` —— 按 `0-C2 §②` 的「不做」，保持应用层容忍启动竞态。
- ❌ 给 `milvus` 加标量索引 / 生产级调参 —— 属适配器（T-A1 已按 `1-2 §⑦-5` 记录为版本限制）与运维范畴。
- ❌ `CG_RAG_TEST_MILVUS_URI` 的真机集成测试跑通（21 passed）—— 属 `0-C3`（T-A4）。
- ❌ `.venv` 内安装 `pymilvus` —— 仍是 extra `milvus` + 系统 Python，**离线基线不受影响**（8 条 skip 逐条不变）。

**下一步建议（精确到卡）**

1. **`0-C3`（T-A4 Milvus 集成测试）**：现在**具备真跑条件**了 ——
   `$env:CG_RAG_TEST_MILVUS_URI="http://localhost:19530"` + 本卡起的服务即可，
   断言 2 / 断言 6 已随 `4bc8f8f` 转正；剩余需补 `1-5 §⑤` 的 7 条断言缺口与 `.venv` 复现。
2. **`0-C4`（T-A4 之后的阈值扫参）**：与本卡无依赖关系。

**本卡遗留的临时物**：当轮探针脚本（`.tmp_t/_ta3_*.py`、`.tmp_t/_ta1_count_fix_check.py`）
与提交信息文件（`.tmp_t/msg_[abc].txt`）**已在本轮全部清理**；
对照实验用的一次性集合 `t_a3_count_probe` / `t_a3_strong_probe` 已在 Milvus 中 `drop`，
**只保留 `t_a3_smoke`**（内含 `doc_id=T-A3-DOC` 一条切片，是 §⑦-4 持久化断言的现场物证，
供 `0-C3` 复核；不影响仓库与离线门禁）。
