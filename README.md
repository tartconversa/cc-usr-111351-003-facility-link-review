# 绿色技术名录的约束实体消歧与可撤销审阅

把技术清单（catalog）、展商名录（expo）与园区台账（park）的来源记录，消歧成
**可审阅、可撤回、可复现**的机构（organization）与基地（facility）实体关联结果，
供后续技术覆盖分析使用。

核心立场：**有依据地识别并保留歧义，而不是把名称去空格后强行去重。**
名称/地址相似度只是软证据；同时有效且注册号不同是硬性不可合并；
机构与基地是不同实体类型，母子关系不等于同一实体。

## 目录

```
src/facility_link_review_input.py   原始资料读取入口（既有，未改动）
src/dedup/
  model.py        记录/地址/有效时段校验，记录指纹与证据版本（不做推断）
  textkit.py      名称规范化、字符级相似度、同音近形弱证据、逐片段地址比较
  blocking.py     阻断键候选生成、漏配诊断、指定来源组合全对核查
  evidence.py     成对证据：硬规则、离散软证据等级、可解释自动判定与政策版本
  cluster.py      受硬约束保护的全局聚类、冲突链、稳定（内容哈希）簇标识
  reviews.py      人工审阅账本：断言/撤回/过期，人工事实与算法分数分离
  publish.py      可复现发布包；统计只消费完整发布的分量
  evaluation.py   调参集/留存集隔离的候选召回与误合率评估
  workflow.py     命令行入口（candidates / publish / rejudge）
fixtures/
  sample.json             既有最小样例
  cases.json              五个对照场景 + 分割标签（无审阅）
  cases_reviewed.json     含有效人工断言与归属确认
  cases_rejudge.json      含撤回与一条过期审阅，用于重判对照
tests/                    unittest 回归（31 个检查，纯标准库）
```

无第三方运行依赖，代码在 Python 3.11+ 运行（未使用更高版本专有语法）。

## 数据契约

每条记录有独立 `record_id`、`source`、`kind`、`name`、可缺失的 `registration`、
有效时段 `valid_from/valid_to`、逐片段 `address`（province/city/district/street/detail）
与可选 `parent_id`。缺失统一为 `null`，**任何相似度都不把缺失当作相等**。

- 空串注册号在解析时归一化为缺失，避免“两个空注册号”被误判相等；
- `kind` 仅允许 `organization` / `facility`，跨类型对永不进入同一聚类；
- 有效时段按闭区间判相交，开放端（null）按无限远处理。

## 判定如何产生（可解释，不输出含义不明的分数）

### 硬性规则（不可被相似度推翻）

| 规则号 | 含义 |
| --- | --- |
| `H0-kind-mismatch` | 机构与基地类型不同，永不合并，只可能形成归属关系 |
| `H1-registration-conflict-while-overlapping` | 有效时段相交且双方注册号都存在但不同 → 硬性不同 |

注册号不同但**时段不相交**（可能跨年更名）不是硬冲突，输出
`S9-registration-diff-in-disjoint-periods` 提示并进入待审，禁止静默合并。

### 软证据（离散等级 + 规则编号）

- 名称：NFKC 归一化、大小写折叠、去空白标点仅用于比较；规范化相等本身不是硬依据。
  字符级 `SequenceMatcher` 比率与同音近形折叠比率取优，映射到 strong/weak/none；
  同音命中（`S2a`）和集团/基地关系字样（`S2b`）只做解释与风险标记。
- 地址：逐片段比较，等级为 identical / compatible / weak / conflict / unknown，
  并报告 coverage（双方都有值的片段占信息更全一方的比例）。
- `soft_score` 是各软维度按权重的归一化加权，**仅用于待审队列排序**；
  全部维度缺失时为 null，不参与排序。它不是阈值开关。

### 自动判定走显式政策规则，不走“分数过线”

- `A1-auto-same-policy`：时段相交、地址不冲突，且名称 strong + 地址支持；
  注册号相等时名称可放宽到 weak。
- `A2-auto-different-policy`：硬冲突，或名称无证据且地址不相同。
- 其余一律 `review`，连同理由编号进入待审队列。
- 跨类型对为 `relation`，归属候选单独成边，需 `assert_affiliation` 人工确认。

政策（权重、名称比率档位、自动判定允许的等级、同音组、`tuned_on_pairs`）
集中在 `evidence.Policy`，带 `config_hash`；政策版本与人工证据版本是两条独立版本线。

## 候选生成与漏配诊断

固定阻断规则集合，每条候选都带产出它的规则名（provenance）：

- 同类型：`name_prefix`、`name_bigram`、`address_district`、`registration_exact`
- 跨类型归属：`affiliation_parent_ref`（显式 parent_id，悬空引用计数告警）、
  `affiliation_name_containment`（机构名包含于基地名且有共有地址片段）

`blocking_diagnostics` 对已知同实体对逐条报告每个阻断规则的漏配原因
（如 `prefix_differs`、`address_incomplete`、`registration_missing`）；
`all_pairs_for_source_combos` 对指定来源组合做小规模全对枚举，用于人工核查召回。

## 全局一致：成对证据如何拼成聚类

聚类用受保护的并查集，同一边只在算法 auto_same 或人工确认同一时并入，
每次并入前检查目标两分量之间是否存在硬性不同/人工确认不同边：

- 存在冲突 → 拒绝并入，两个分量整体**冻结**，输出完整冲突链
  （在“所有同一诉求边”构成的诉求图上，连接硬性不同边两端点的传递路径），
  人工同一链与人工确认不同相撞时人工边优先展示，任何人工意见都不被丢弃；
- A≈B、B≈C 不能越过 A、C 之间的硬冲突——桥接误合的分量停止发布而非强行合并；
- 簇标识是成员集合的 SHA256 内容哈希，与遍历顺序无关；成员集合不变则标识不变。

冻结分量不产生簇标识、不进统计；待审对不阻断发布，但单独成队列且不参与统计。

## 审阅账本与撤回重判

事件词汇：`assert_same` / `assert_different` / `withdraw`
（跨类型用 `assert_affiliation` / `withdraw_affiliation`）。

- 每条断言绑定明确记录对、`review_id` 与 `evidence_version`；
  证据版本由两条记录的身份字段内容哈希派生，名称/地址/注册号/时段/层级任一变化即换版；
- 资料更新导致版本不匹配的旧审阅进入 `expired`，**不覆盖新资料、不参与聚类**，等待重新确认；
- 撤回事件用独立 `review_id` + `target_review_id` 指向原断言，撤回与断言都留痕；
- 撤回后相关分量重新判定，`rejudge` 输出旧簇→新簇的成员变化，
  并校验无关分量标识零漂移。

## 可复现发布与统计纪律

发布包由资料指纹、政策配置、审阅轨迹与确定性算法决定，不含时间戳/环境路径/随机数，
整体带 `publication_hash`，可按资料与审阅版本复现。`stats` 只消费完整发布分量，
冻结成员、待审成员与过期审阅一律排除，并显式标注口径。

## 评估纪律（防止用留存集调阈值后再声称独立表现）

标签分两个不相交分割：

- `tuning`：只用于候选召回、阻断漏配诊断和误合分析，可据此改政策；
- `heldout`：只用于一次性独立评估。代码强制：两分割对重叠即报错；
  留存对出现在政策 `tuned_on_pairs` 中直接拒绝出报告。

留存指标：候选召回率、自动判定命中、误合对清单与误合率
（在留存集显式标注的 different 标签宇宙内核算）。

## 命令行用法

`-I` 隔离模式下脚本自行引导仓库路径，不依赖 cwd 注入，也不读取被 planted 的本地模块：

```bash
# 候选报告 + 来源组合全对核查 + 调参集阻断诊断
python3 -I src/dedup/workflow.py candidates fixtures/cases.json out/cand.json \
    --all-pairs catalog,expo --all-pairs expo,park --labels fixtures/cases.json

# 全量发布（--labels 指向含 heldout 的标签时附带留存评估）
python3 -I src/dedup/workflow.py publish fixtures/cases_reviewed.json out/pub.json \
    --labels fixtures/cases_reviewed.json

# 撤回后的重判：对比旧发布包，输出簇成员变化与无关分量漂移检查
python3 -I src/dedup/workflow.py rejudge fixtures/cases_rejudge.json out/pub.json out/re.json
```

运行测试与编译检查：

```bash
python3 -I -c "import sys, unittest; sys.path.insert(0, '.'); \
  unittest.TextTestRunner(verbosity=1).run(unittest.TestLoader().discover('tests', top_level_dir='.'))"
python3 -m compileall -q src tests
```

## 五个对照场景（见 fixtures/cases.json）

1. **同音近名**：H1/H2 同音且同注册号 → 自动同一；H1/H3 名称几乎相同但同时有效、
   注册号不同 → 硬性不同（对应“一家换名被算三家”与“名字像被并一家”两类原始事故）。
2. **跨年更名**：N1/N2 时段不相交、名称 strong → 进待审，人工 assert_same 后才合并。
3. **地址搬迁**：M1/M2 同注册号但地址 conflict → 进待审；撤回人工确认后退回两个簇。
4. **集团/基地混淆**：G1（机构）与 G2/G3（基地）跨类型只建归属边；
   G2/G3 名字相似但注册号不同 → 硬性不同，不会被相似规则合成一个。
5. **桥接误合**：B1≈B2、B2≈B3 但 B1/B3 注册号硬冲突 → 整个分量冻结、列冲突链、停止发布。
