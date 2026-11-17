# Treasure Map Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增可供 CLI 和未来微信 Bot 复用的藏宝图布局匹配、拒识及去重收录模块。

**Architecture:** `features.py` 负责中央区域图片与 SIFT 特征缓存，`matcher.py` 负责双向匹配和几何校验，`library.py` 负责候选排序、保守判定及原子收录，`cli.py` 只处理参数和结构化结果展示。图像算法不依赖微信模块，本阶段不修改 Bot 链路。

**Tech Stack:** Python 3.10+、Pillow、NumPy、OpenCV SIFT（`opencv-python-headless>=4.8,<4.12`）、pytest。

**Spec:** [藏宝图匹配与去重收录设计](../specs/2026-11-17-treasure-map-matching-design.md)

## Global Constraints

- 图库默认是父仓库 `images/`；现有图片和 `manifest.json` 不修改，新图只写 `images/added/`。
- 缓存只放 Git 忽略的 `data/treasure_map/`；测试收录只用临时目录，不把 `input_img/` 样本加入正式图库。
- 找不到和已重复是正常结果；证据不足不得强行返回最佳候选。
- 本期不接微信、不发送消息、不启动或重启 Bot，不自动提交 Git。
- 保留父仓库、子模块已有修改，不做无关重构或格式化。

## Review Focus

- 万圣节皮肤、不同分辨率和未开格不能破坏同布局匹配：Task 1 构造缩放/背景/遮挡测试，Task 4 运行三张真实样本并人工核对候选。
- 重复道具造成的错误位置匹配必须被拒识：Task 1 比较不同布局分数，Task 2 固定内点、覆盖率和前两名差距边界。
- 弱匹配或有歧义的中等匹配必须返回 `not_found`：Task 2 测试绝对门槛和 15% 歧义规则。
- 重复收录不得写文件，新图写入失败不得留下目标文件或临时文件：Task 2 测试完全重复、布局重复及 `os.replace` 失败。
- 空图、坏图和空特征不得导致异常 traceback 或伪造匹配：Task 1 测试空特征及坏图，Task 3 测试 CLI 错误退出码。

---

### Task 1: SIFT 特征、缓存与几何比较

**Files:**
- Create: `src/treasure_map/models.py`, `src/treasure_map/features.py`, `src/treasure_map/matcher.py`
- Modify: `requirements.txt`
- Test: `src/tests/test_treasure_map_features.py`

**Interfaces:**
- `FeatureSet(source: Path, sha256: str, points: np.ndarray, descriptors: np.ndarray, shape: tuple[int, int])`
- `Similarity(good_matches: int, inliers: int, inlier_ratio: float, coverage: int, score: float)`
- `FeatureExtractor.extract(path: Path, cache_dir: Path | None = None) -> FeatureSet`
- `SiftComparator.compare(query: FeatureSet, reference: FeatureSet) -> Similarity`
- `TreasureMapError(RuntimeError)`

- [ ] **Step 1: 添加依赖并写失败测试**

在 `requirements.txt` 增加 `opencv-python-headless>=4.8,<4.12`，只向项目 `.venv` 安装该依赖。测试用固定随机种子生成含多个有纹理道具的场景，覆盖：顶部/底部变化不影响特征；同布局经缩放、换背景和局部遮挡后内点至少 12 且分数高于不同布局；空白图返回零相似度；坏图抛出 `TreasureMapError`；相同内容第二次提取命中缓存且文件变化后重新提取。

```python
assert same_layout.inliers >= 12
assert same_layout.score > different_layout.score
assert blank.inliers == blank.good_matches == 0
```

- [ ] **Step 2: 运行测试确认先失败**

Run: `.venv/bin/python -m pytest src/tests/test_treasure_map_features.py -q`
Expected: 因 `src.treasure_map` 尚不存在而 FAIL。

- [ ] **Step 3: 实现模型、特征提取与比较**

图片经 Pillow 校验、EXIF 校正并转 RGB；裁剪全宽、纵向 `0.24～0.82`，宽度归一化为 828，屏蔽棋盘上方横幅和未开格。SIFT `nfeatures=2500`、`contrastThreshold=0.02`。缓存文件名包含缓存版本和源文件 SHA-256，使用 `allow_pickle=False` 可读的 NPZ 并原子写入。

比较使用 Lowe 比率 `0.8` 的双向匹配，仅保留互为最佳匹配的点；几何校验只允许水平 ±25、垂直 -150～100 的共同平移，误差不超过 6 像素。将查询内点投影到 8×8 网格计算 `coverage`，`inlier_ratio = inliers / good_matches`，每个区域最多贡献 5 个计分内点，`score = 区域限分内点数 * inlier_ratio`；无足够特征或共同平移时保留初筛统计并令内点、覆盖和分数为零。

- [ ] **Step 4: 运行特征测试**

Run: `.venv/bin/python -m pytest src/tests/test_treasure_map_features.py -q`
Expected: 全部 PASS，且 `cv2.__version__` 为 4.8～4.11，NumPy 仍为 1.24.3。

### Task 2: 图库查找、拒识与原子收录

**Files:**
- Create: `src/treasure_map/library.py`
- Test: `src/tests/test_treasure_map_library.py`

**Interfaces:**
- `Thresholds` 固定字段：`min_inliers=12`、`min_coverage=2`、`min_inlier_ratio=0.20`、`min_score=30.0`、`strong_score=60.0`、`ambiguity_ratio=0.15`、`duplicate_min_inliers=60`、`duplicate_min_coverage=12`、`duplicate_min_inlier_ratio=0.35`、`duplicate_min_score=40.0`。
- `MatchResult(status: Literal["found", "not_found"], path: Path | None, similarity: Similarity | None, runner_up: Similarity | None = None)`
- `AddResult(status: Literal["added", "duplicate"], path: Path, similarity: Similarity | None = None)`
- `TreasureMapLibrary(library_dir: Path, cache_dir: Path | None = None, *, extractor=None, comparator=None, thresholds: Thresholds | None = None)`
- `match(image_path: Path) -> MatchResult`；`add(image_path: Path) -> AddResult`

- [ ] **Step 1: 用假特征提取器和比较器写失败测试**

覆盖递归扩展名扫描、空图库找不到、最佳候选返回、绝对阈值拒识、中等结果前两名差距不足 15% 拒识、强结果不受歧义限制、完全重复不调用比较器、布局重复不写入、仅达到部分图阈值不能判全开重复、新图以 `<sha256><suffix>` 保存且字节一致、`os.replace` 失败无残留。

```python
assert library.match(partial).status == "not_found"
assert library.add(full_duplicate).status == "duplicate"
assert not (library_dir / "added").exists()
```

- [ ] **Step 2: 运行图库测试确认先失败**

Run: `.venv/bin/python -m pytest src/tests/test_treasure_map_library.py -q`
Expected: 因 `library.py` 尚不存在而 FAIL。

- [ ] **Step 3: 实现图库与收录**

递归扫描 `.png/.jpg/.jpeg/.webp` 文件并缓存各图特征。`match` 按 `score`、`inliers` 降序排列，只在绝对门槛全部通过时考虑候选；最佳为中等强度且第二名相对差距不足 15% 时返回 `not_found`，否则返回最佳。`add` 先查相同 SHA-256，再用全开重复阈值检查所有候选；重复返回已有路径且不创建 `added/`，新图通过同目录临时文件和 `os.replace` 原子保存，异常清理临时文件并抛 `TreasureMapError`。

- [ ] **Step 4: 运行图库测试**

Run: `.venv/bin/python -m pytest src/tests/test_treasure_map_library.py -q`
Expected: 全部 PASS，测试结束后临时图库无意外文件。

### Task 3: Python 公共 API 与 CLI

**Files:**
- Create: `src/treasure_map/__init__.py`, `src/treasure_map/cli.py`, `src/treasure_map/__main__.py`
- Test: `src/tests/test_treasure_map_cli.py`

**Interfaces:**
- `main(argv: Sequence[str] | None = None, library: TreasureMapLibrary | None = None) -> int`
- 命令：`python -m src.treasure_map match IMAGE [--json]`、`python -m src.treasure_map add IMAGE [--json]`；可选全局 `--library`、`--cache-dir` 覆盖默认路径。
- 公共导出：`TreasureMapLibrary`、`MatchResult`、`AddResult`、`Similarity`、`TreasureMapError`。

- [ ] **Step 1: 写 CLI 失败测试**

注入假 library，分别断言找到、找不到、已重复、已加入的退出码均为 0，中文文本包含对应状态及路径；`--json` 可解析且 `status/path/similarity` 与结构化结果一致；library 抛 `TreasureMapError` 时 stderr 有简洁错误且退出码为 2，不显示 traceback。

- [ ] **Step 2: 运行 CLI 测试确认先失败**

Run: `.venv/bin/python -m pytest src/tests/test_treasure_map_cli.py -q`
Expected: 因 CLI 模块尚不存在而 FAIL。

- [ ] **Step 3: 实现参数解析、输出和公共导出**

默认图库由 `cli.py` 的父仓库位置推导为 `images/`，默认缓存为子模块 `data/treasure_map/`。`not_found` 文本固定为“找不到足够相似的藏宝图”，`duplicate` 以“已重复：”开头，`added` 以“已加入：”开头。JSON 使用 `ensure_ascii=False`，Path 转字符串，Similarity 转字段字典；业务错误统一写 stderr。

- [ ] **Step 4: 运行 CLI 及新模块全部测试**

Run: `.venv/bin/python -m pytest src/tests/test_treasure_map_features.py src/tests/test_treasure_map_library.py src/tests/test_treasure_map_cli.py -q`
Expected: 全部 PASS。

### Task 4: 真实样本校准、回归与模块导航

**Files:**
- Modify as needed: `src/treasure_map/models.py`, `src/treasure_map/matcher.py` and corresponding tests
- Modify: `docs/02-architecture/MODULE_INDEX.md`, `../AGENTS.md`, `docs/superpowers/plans/2026-11-17-treasure-map-matching.md`

**Interfaces:**
- 保持 Task 1～3 公共 API 不变；阈值调整必须同时更新 `Thresholds`、对应边界测试和本计划完成记录。

- [ ] **Step 1: 校准三张真实未全开样本**

Run:
```bash
.venv/bin/python -m src.treasure_map match ../input_img/input1.png --json
.venv/bin/python -m src.treasure_map match ../input_img/input2.png --json
.venv/bin/python -m src.treasure_map match ../input_img/input3.png --json
```
Expected: 每条命令正常结束；对返回的候选用图片查看工具人工核对道具和位置。若结果错误，先保存诊断并检查匹配与几何实现，再依据证据调阈值并添加回归，不通过删除拒识测试制造通过。若返回找不到，需核对最高分候选后才能接受。

- [ ] **Step 2: 验证原图、变换图、拒识和临时收录**

对至少每个帖子目录的一张代表图做自匹配；在临时目录生成缩放、JPEG 压缩和遮挡版本，确认能找回原图，并抽查不同布局不会通过阈值。用临时图库验证重复 add 无写入、新图 add 完整写入；不得对正式 `images/` 执行 add。

- [ ] **Step 3: 更新导航并完成记录**

在模块索引说明 `src/treasure_map/` 的职责和 CLI；在父仓库 `AGENTS.md` 增加独立模块导航及“尚未接入 Bot”。在本计划勾选完成步骤并记录三张输入的实际结果、校准后阈值和验证范围。

- [ ] **Step 4: 全量验证和变更检查**

Run:
```bash
.venv/bin/python -m pytest src/tests -q
git diff --check
git status --short
```
Expected: 测试全部 PASS，diff 无空白错误；状态只包含本任务及用户原有修改。另在父仓库运行 `git diff --check`、`git status --short`，确认未覆盖已有内容、未产生提交。
