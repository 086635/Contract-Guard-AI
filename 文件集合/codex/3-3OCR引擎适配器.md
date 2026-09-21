# 3-3 · OCR 引擎适配器（T-C2）

> 所属模块：3 · OCR　公共规范见 `0.施工标准与定义归属.md`（下称"标准"）
> 模块总纲：`3.OCR模块.md` §3.3（引擎层 `ocv/{engine,seal}.py`）/ §3.4.2（**坐标体系：渲染 DPI 不能丢**）

## ① 元信息

| 项 | 值 |
|---|---|
| 任务 ID | **T-C2** |
| 优先级 | **P0**（关键路径第二环：没有它，扫描件支路整条断掉） |
| 人日 | 1.0 |
| 前置依赖 | **T-C0**（引擎已装且可用）；消费方为 **3-5**（坐标索引）与 **3-6**（接线） |
| 影响面 | `doc_processor/ocr/engine.py`（新建）、`doc_processor/ocr/__init__.py`（只增导出）、`doc_processor/errors.py`（增一个异常）、`tests/` |
| 是否触碰契约层 | ❌ 否（`OcrResult` / `SealDetection` **字段冻结**；新增实现类不改契约） |

---

## ② 范围

### ✅ 做

- 新建 `doc_processor/ocr/engine.py`：`LocalOcrEngine`（`OcrEngine` 的**真实实现**）
- **延迟导入**：引擎 import 全部写在方法内部，缺引擎时**包仍可导入**
- **结构兼容层**：一套 `_iter_lines()` 同时吃 PaddleOCR 2.x / 3.x / RapidOCR 三种返回结构
- **渲染**：`pypdfium2` 把 PDF 页渲染为图（`render_dpi`），**禁止 `pdf2image`**
- **坐标归一**：所有输出坐标经 `BBoxAligner.to_pdf_points(..., src_dpi=...)` 转 `PDF_POINTS`
- 置信度过滤、越界框过滤、单页超时、结果排序（按 `page, y, x`）
- `build_ocr_engine()` 工厂 + `detect_engine()` 探测（供 `scripts/check_ocr_env.py` 与 `3-6` 复用）
- `detect_seals()` 委托 **3-8** 的 `seal.py`；3-8 未落盘时返回 `[]` + warning（**不抛异常**）

### ❌ 不做

- ❌ 不做要素/条款抽取（属 **3-4**）；不做版面分析（属 **3-7**）
- ❌ 不改 `OcrResult` / `SealDetection` 字段（契约冻结；需要坐标体系信息时用 `aligner` 的 `NormalizedBBox`）
- ❌ 不识别手写签名（D3-d 建议本次只做"存在性检测"，属 3-8）
- ❌ 不在 `__init__.py` 顶层 import 引擎；不引入 `torch` / `pdf2image`
- ❌ 不做多语言自动切换（`lang` 由配置给，默认 `ch`）

---

## ③ 定义归属

| 本文件**定义**（唯一处） | 本文件**引用**（不重复定义） |
|---|---|
| `LocalOcrEngine` / `build_ocr_engine()` / `detect_engine()`（`ocr/engine.py`） | `OcrEngine` / `OcrResult` / `SealDetection`（`ocr/__init__.py`，**冻结契约**） |
| `render_pdf_page()`（PDF → 图像，**唯一渲染入口**） | `BBoxAligner.to_pdf_points()`（`aligner/__init__.py`，**已实现，禁止另写缩放**） |
| **`.iter_lines()` 结构兼容层**（三种返回结构 → `(box, text, score)`） | `OcrSettings`（`config.py`，**3-2 定义**：`engine` / `render_dpi` / `image_dpi` / `lang` / `min_confidence` / `model_dir` / `timeout_s`） |
| `OcrEngineUnavailableError` / `RenderFailedError`（`errors.py`，均继承 `DocProcessorError`） | `DocProcessorError`（`errors.py`，**3-2 定义**） |
| **页级超时策略**与已知限制（§⑥.6） | 埋点 API（`cg_observability`，标准 §0.5.3） |

---

## ④ 交付物

| 文件 | 动作 | 职责 |
|---|---|---|
| `packages/doc-processor/doc_processor/ocr/engine.py` | **新建** | `LocalOcrEngine` / `build_ocr_engine()` / `detect_engine()` / `render_pdf_page()` |
| `packages/doc-processor/doc_processor/errors.py` | 修改 | 增 `OcrEngineUnavailableError` / `RenderFailedError` |
| `packages/doc-processor/doc_processor/ocr/__init__.py` | 修改 | **只增导出**（`LocalOcrEngine` / `build_ocr_engine`）；契约类一字不改 |
| `packages/doc-processor/tests/test_ocr_engine.py` | **新建** | 结构兼容层、DPI 归一、过滤、超时、降级（§⑧） |

---

## ⑤ 接口规格

```python
# doc_processor/ocr/engine.py
class LocalOcrEngine(OcrEngine):
    """本机 OCR 引擎实现（延迟导入 + 多引擎兼容）。

    设计要点：
    1. **契约冻结**：只实现基类的 `ocr_page` / `detect_seals`，不改 `OcrResult` 字段。
    2. **坐标零例外**：任何来源的像素坐标都必须经 `to_pdf_points(..., src_dpi=...)`。
    3. **缺引擎不崩包**：引擎 import 在方法内完成，失败时抛 `OcrEngineUnavailableError`。
    """

    def __init__(self, settings: OcrSettings | None = None) -> None: ...

    @property
    def engine_name(self) -> str:
        """返回实际生效的引擎名（`rapid` / `paddle` / `none`）。"""

    def ocr_page(self, image_path: str) -> list[OcrResult]:
        """对单张图片做 OCR（图片按 `settings.image_dpi` 解释）。"""

    def ocr_image(self, image: "np.ndarray", *, page: int, src_dpi: int) -> list[OcrResult]:
        """对内存图像做 OCR（**工作马**；`ocr_page` 与 PDF 渲染路径都走它）。"""

    def ocr_pdf_page(self, pdf_path: str, page_index: int) -> list[OcrResult]:
        """渲染 PDF 指定页（`render_dpi`）后 OCR。"""

    def detect_seals(self, image_path: str) -> list[SealDetection]:
        """印章检测（委托 `ocv/seal.py`；该文件未落盘时返回 [] + warning）。"""


def detect_engine(settings: OcrSettings | None = None) -> str:
    """探测可用引擎：`"rapid"` / `"paddle"` / `"none"`（**不抛异常**）。"""


def build_ocr_engine(settings: OcrSettings | None = None) -> OcrEngine:
    """按配置构造引擎。

    Raises:
        OcrEngineUnavailableError: `detect_engine()` 返回 `"none"`。
    """


def render_pdf_page(
    pdf_path: str, page_index: int, *, dpi: int
) -> tuple["np.ndarray", int, int]:
    """把 PDF 页渲染为 BGR 图像（**唯一渲染入口**）。

    Returns:
        `(image, width_px, height_px)`；图像为 `numpy` BGR（与 opencv 对齐）。

    Raises:
        RenderFailedError: 页索引越界或文件损坏。
    """
```

**结构兼容层（本任务的技术核心）**：

```text
输入可能是三种形状：
  A · PaddleOCR 2.x ：ocr(img, cls=True) → [ [ [box, (text, score)], ... ] ]      ← 多一层页嵌套
  B · PaddleOCR 3.x ：predict(img)       → [ {"rec_texts": [...], "rec_scores": [...],
                                              "rec_polys" | "dt_polys": [...]} ]
  C · RapidOCR      ：engine(img)        → ( [ [box, text, score], ... ], elapse )

统一出口：`_iter_lines(payload) -> Iterator[tuple[Sequence[Sequence[float]], str, float]]`
  · `box` 是**四点坐标**（左上、右上、右下、左下）；
  · 取外接矩形 (min_x, min_y, max_x, max_y) 得到 `x/y/w/h`；
  · 任何无法识别的形状 → 记 warning 并跳过，**不抛异常**（防某一版引擎改结构把整条链路打挂）。
```

---

## ⑥ 实现规范

1. 🔴 **延迟导入**：`_impl` 在首次调用时构造并缓存；`import paddleocr` / `import rapidocr_onnxruntime` **只允许出现在 `engine.py` 的函数体内**（源码级断言见 §⑦.6）。
2. 🔴 **DPI 归一（本模块最致命的坑）**：
   - PDF 渲染路径：`src_dpi = settings.render_dpi`；
   - 独立图片路径：`src_dpi = settings.image_dpi`（默认 72 —— 即"1 像素 = 1 点"）；
   - **必须**调用 `to_pdf_points()`，**禁止**手写 `* 72 / dpi`。
   > 丢了 DPI → 所有框整体错位，且**不会报错**，只在演示时肉眼可见。
3. 🔴 **四点 → 外接矩形**：`x = min(xs)`、`y = min(ys)`、`w = max(xs) - x`、`h = max(ys) - y`。
4. 🔴 **过滤规则**：`score < settings.min_confidence` 丢弃；框出界（含 2pt 容差）丢弃；两者都记 warning 计数。
5. 🔴 **排序**：结果按 `(page, round(y, 1), x)` 排序，保证同一页文本顺序稳定（下游 `3-4` 依赖顺序切条款）。
6. 🔴 **页级超时（含已知限制）**：用 `ThreadPoolExecutor(1)` + `future.result(timeout=settings.timeout_s)`；
   超时后**丢弃该页结果**并记 `log_event("ocr.page.timeout", level="WARNING")`。
   ⚠️ **已知限制**：Python 无法强杀线程，被超时的引擎调用仍在后台跑完（占 CPU）。
   该限制**必须写进本文件回填表**，不要假装"已经中断"。
7. 🔴 **缺引擎必须显式**：`build_ocr_engine()` 在 `detect_engine()=="none"` 时抛 `OcrEngineUnavailableError`
   ——**不要**返回一个"什么都不做"的空引擎（会让 3-6 静默产出空结果，比崩更糟）。
8. 🔴 **引擎实例缓存**：一个 `LocalOcrEngine` 只构造一次底层引擎（构造开销大，动辄数秒）。
9. ⚠️ **图像格式**：优先把 `numpy BGR` 交给引擎；若引擎只吃路径，则落临时文件（`tempfile`）并在 `finally` 删除。
10. ⚠️ **日志不打图像数据**：只记 `page` / `lines` / `elapsed_ms` / `engine`（numpy 数组打印会让日志爆炸）。

### 实测后回填（DoD 要求）

| 项 | 实测值（落地时填写） |
|---|---|
| 引擎名与版本（`engine_name`） | ⬜ 待回填 |
| A4 / `render_dpi=200` 单页 OCR 耗时（P50 / P95） | ⬜ 待回填 |
| 平均识别行数与平均置信度 | ⬜ 待回填 |
| 超时线程"后台跑完"的实测影响（CPU / 内存） | ⬜ 待回填 |
| 结构兼容层实测命中的分支（A/B/C） | ⬜ 待回填 |

---

## ⑦ 验收标准（**逐条可执行**）

> 环境前缀见 `AGENTS.md` §4.1。除断言 2/3 需引擎可用外，其余**离线可跑**。

**断言 1 · 探测引擎**

```powershell
D:\aanacoda1\python.exe -c "from doc_processor.ocr.engine import detect_engine; n=detect_engine(); print(n); assert n in ('rapid','paddle')"
```

**断言 2 · 对图片出字且坐标在页面内**

```powershell
D:\aanacoda1\python.exe -c "from doc_processor.ocr.engine import build_ocr_engine; r=build_ocr_engine().ocr_page('packages/doc-processor/tests/fixtures/scan.png'); print(len(r), r[:1]); assert r and all(x.x>=0 and x.y>=0 for x in r)"
```

**断言 3 · DPI 归一正确（核心）**

```powershell
D:\aanacoda1\python.exe -c "from doc_processor.aligner import BBoxAligner; b=BBoxAligner().to_pdf_points(1, 300, 600, 300, 60, src_dpi=300); print(b.x, b.y, b.width, b.height); assert (b.x,b.y,b.width,b.height)==(72.0,144.0,72.0,14.4)"
```

**断言 4 · 结构兼容层（纯函数，不需要引擎）**

对 A/B/C 三种假 payload 调 `_iter_lines()`：均得到 `[(box, text, score)]`；非法形状 → 空列表 + warning，不抛异常。

**断言 5 · 引擎缺失时显式报错**

```powershell
D:\aanacoda1\python.exe -c "import sys; sys.modules['rapidocr_onnxruntime']=None; from doc_processor.errors import OcrEngineUnavailableError; print('error type ok')"
```

（完整校验放在用例里：`monkeypatch` 使 `detect_engine()` 返回 `"none"` → `build_ocr_engine()` 抛 `OcrEngineUnavailableError`。）

**断言 6 · 包顶层无引擎 import**

```powershell
D:\aanacoda1\python.exe -c "import io, pathlib; bad=[]; [bad.append(str(p)) for p in pathlib.Path('packages/doc-processor/doc_processor').rglob('*.py') if any(s in io.open(p,encoding='utf-8').read()[:2000] for s in ('import paddleocr','import rapidocr_onnxruntime'))]; print(bad); assert not bad"
```

**断言 7 · 契约未变**

```powershell
D:\aanacoda1\python.exe -c "from doc_processor.ocr import OcrResult, SealDetection; import dataclasses as d; assert [f.name for f in d.fields(OcrResult)]==['page','x','y','width','height','text','confidence']; assert [f.name for f in d.fields(SealDetection)]==['page','x','y','width','height','extracted_text','confidence']; print('contract frozen ok')"
```

**断言 8 · 测试基线**：`D:\aanacoda1\python.exe -m pytest -q` → **≥ 140 passed**。

---

## ⑧ 测试要求

`packages/doc-processor/tests/test_ocr_engine.py`：

| 用例 | 断言 |
|---|---|
| `test_detect_engine_available` | §⑦ 断言 1 |
| `test_ocr_page_returns_boxed_lines` | §⑦ 断言 2（夹具：`scan.png`，由 `3-2` 的夹具脚本生成） |
| `test_iter_lines_handles_three_shapes` | §⑦ 断言 4（表驱动：A/B/C 三种 payload + 1 个畸形 payload） |
| `test_iter_lines_malformed_is_empty` | 畸形 payload → `[]` 且不抛 |
| `test_dpi_normalization_pdf_vs_image` | `render_dpi=200` 与 `image_dpi=72` 两条路径的换算分别正确 |
| `test_low_confidence_filtered` | 注入 `score=0.1` → 被丢弃 + warning |
| `test_out_of_page_box_filtered` | 注入出界框 → 被丢弃 |
| `test_engine_unavailable_raises` | §⑦ 断言 5（monkeypatch `detect_engine`） |
| `test_timeout_marks_page_skipped` | 桩引擎 sleep 超时 → 该页返回 `[]` + `ocr.page.timeout` 记录（**用注入的假 `timeout_s=0.01`**） |
| `test_no_engine_import_at_top_level` | §⑦ 断言 6 |
| `test_contracts_unchanged` | §⑦ 断言 7 |

> 🔴 需要真实引擎的用例（断言 2 / 6 组）在**无引擎机器**上仍应 **fail**（本任务的定义就是引擎可用）；
> 仅"第二引擎"相关用例才允许 `skipif`（标准 §0.6.2）。

---

## ⑨ 避坑清单

| # | 坑 | 说明 |
|---|---|---|
| 1 | 忘记 DPI 归一 | 框整体错位且**不报错**，只有演示时肉眼发现（模块总纲 §3.4.2 点名） |
| 2 | 把四点坐标当 `(x, y, w, h)` | 会得到错误的宽度/高度；必须取外接矩形 |
| 3 | PaddleOCR 2.x 少解一层嵌套 | `[[ [box,(text,score)], ... ]]` 外层是"页"，漏解就是空结果 |
| 4 | 在顶层 `import paddleocr` | 缺内核时整个包不可导入（现状 F4 的连锁反应） |
| 5 | 独立图片沿用 `render_dpi` 当 DPI | 图片 DPI 与渲染 DPI 是两件事；用 `image_dpi`（默认 72） |
| 6 | 超时后宣称"已中断引擎" | 线程杀不掉；必须如实登记该限制 |
| 7 | 缺引擎时返回空引擎 | 会让下游静默产出空结果；**必须抛 `OcrEngineUnavailableError`** |
| 8 | 每页重新构造引擎 | 构造开销数秒级；实例必须缓存 |
| 9 | 把 `numpy` 数组写进日志 | 日志体积爆炸、且含合同原文（隐私） |
| 10 | 手写缩放公式 | 必须复用 `to_pdf_points()`（唯一处，标准 §0.7） |