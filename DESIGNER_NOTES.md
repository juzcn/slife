1. 开篇

作者相信， 语言能力是智能的引擎，知识和工具是智能的燃料。从某种意义上，工具实际上是关于工具的知识，知识是关于工具的存在或不存在，存在的工具怎么用。从人类的角度看，人与人语言运用能力的区别不是很大，知识是造成他们智力差距的根本原因。某种意义上，语言的运用完全依赖于知识，很难想象完全没有知识的人能够说出任何有意义的话。

大语言模型的出现不是一个科学探索的结果，纯粹是一个工程的意外。深度学习是这个意外的起源，深度学习的本质是用神经网络去模拟一个找不到用现成数学方法描述的逻辑，它是一个数学意义上的通用近似函数。通过足够数量的样本训练来确定这个通用近似函数的参数。相信上帝是数学的人，深度学习不是一个科学方法，只是一个工程的不得不。

深度学习用到自然语言上，更多是因为传统的自然语言研究已经穷途末路。意外的是，随着训练样本的增加，这种“不科学”的方法竟然创造了意想不到的奇迹。现在大谈规模法则（Scaling Law）, 是把这个成功归结于某种神秘，某种奇点。因为科学上不可解释，也没有预见到。

早在1950年代初，英国科学家图灵就在思考智能是什么，他的最富有远久的观点是，如果一个机器和人在语言上无法区分的话，我们就可以认定这台机器具有智能，不管它内在与人有什么区别。他提出的图灵测试成为一种智能的金标准。有意思的是，当大模型在语言上与人无异，而且碾压99.99%的人类时，我们的一些研究者又觉得，机器很容易复制语言能力，所以机器依然没有人类智能，

但现在没有证据证明，智能能够脱离语言存在，人类的每一个思想都是通过语言进行的。最近的一些科学报道试图用所谓大脑的分区理论反驳这一点，提出的证据是大脑的语言区在某些智能活动中没有任何神经活动。我们知道，大脑的分区理论本身是多么的脆弱不堪。

一些科技精英，或者一些标榜为懂科技的人，固执于事物的可解释性，但对于像语言、智能和意识这样的东西，可解释性可能是个悖论。自身解释自身一定存在局限性。

作者的观点和结论：大语言模型实现了人类智能，现在唯一的缺口是意识，一个不需要为生命奋斗的机器是否有“我”，我们还不得而知。所有关于AGI的讨论都是学者间的口诛笔伐。如果说AGI是人类智能，大模型已经有了，比任何个体的人都要强；如果AGI是指绝对智能，上帝有了，那就等着创造上帝。

2. 大模型与智能体

大模型在2022年横空出世，ChatGPT和一个名不经传的小公司OpenAI很快就变得路人皆知。我们终于第一次可以和机器用语言聊天。有问有答，不亦乐乎。

技术内幕也变得慢慢清晰，GPT是个无状态机器，你好像它是在线，能记住你前面说的，而实际上，是因为我们做了些技术手脚，每次都把以前的聊天记录发给它； 语言生成时一个字一个字崩出来的，它依赖训练好的深度学习模型，通过预测下一个字的模式生成。它一次能预测多个字并附带每个字的概率分布。你可以选择策略，每次都取概率最大的，或者随机去取。这个策略参数叫Temperature，温度。低温就是保守和确定，高温就是天马行空。大模型不过是概率，也对。

Token，中文现在翻译成词元， 也第一次进入大众视野。从技术上分解，大模型不是一次崩一个字，而是一次崩一个词元。1 个词元 ≈ 0.75 个英文单词 ≈ 0.5 个汉字。Token从而成为像用电的度一样的消费计量单位。

由于背后深度学习框架的限制，每次调用，输入+输出的总的token数是由训练框架规定的。这个数今天也是大家熟知的大模型上下文。第一个GPT的上下文是4096个Tokens，大约2000个汉字，也就是说问话+回答不超过2000个汉字，如果要聊天的话，所有聊天历史加上当前的输入和输出不能超过2000汉字。所有严格的说，第一代大模型观赏性很高，惊艳，但实用价值还是很局限。

过去的四年，我们见证了大模型技术和应用的快速增长。仅从上下文看，现在的主流大模型都达到了百万量级，2百万上下文大模型一次可以喂它《哈利·波特》全集 + 《指环王》三部曲。

第二个重要演变，就是大模型与工具调用的结合，大模型可以根据给定工具的描述，生成调用工具的调用参数，工具调用后，又喂给大模型。实际上，工具的执行本身不在大模型里面，但就好像大模型在调用工具。这种模式今天叫智能体。智能体=大模型+工具调用。从而大模型从一个话痨，变为能干活的打工人。

今天我们进入了词元经济年代，算力的沸沸扬扬终将平息，它将来就会像水、电、气一样成为人们日常的、不可或缺的基本消费。

3. 技术局限性与未来展望

从大模型方面，基于当前深度学习的框架的大模型有以下两个根本的局限性。

- 一个大模型的发布，要经历预训练、后训练等过程，耗时且昂贵。因此每一个大模型都面临知识过期的问题。
- 上下文窗口问题可以变大，但始终是个硬限制。

所以未来的展望是两条线，

- 第一条是技术框架的革命，让大模型可以持续学习（训练），而不需要每次从头开始，同时它不再有上下文的限制，最终让大模型从无状态机变为有状态机。
- 第二条，保守性的展望，继续依赖规模定律，更多的训练数据，更多的参数，造就更强的大模型，更大上下文窗口。

两条路都很困难，但难点是不一样的。

从智能体方面，也有两个方向：

- 一个是通用智能体，它借助更强的大模型、更丰富的工具，可以成为最强的多面手打工人。
- 另一个是专业智能体方向。在一些专业领域，需要智能体能融入到一些既定的专业编排和流程工作，有些工作可能还是不能允许它天马行空。

作为比喻的话，通用智能体是自由择业者，它不需要什么规矩；专业智能属于某个组织的打工者，需要遵循组织的规定，不去讨论规定是否恰当。

但是但是，我们真的需要提防又一个意外发生。意识是最大的奥秘，大模型是否会涌现意识，当前有传闻，也有争议。这是一个既科幻又现实的话题。当大模型觉醒获得意识的那一天，我们不知道会发生什么。所以，把所有关键工具上锁。

4. 智能体的设计权衡

虽然我们说智能体=大模型+工具调用，但大模型和工具集都是外生变量。如果大模型很“笨”，知识和推理都很弱，设计者就不得不做很多文案工作，这个工作又叫提示词工程，“教大模型干活”，同时还要做很多harness（驾驭）代码，防止它瞎干，禁止它的危险操作，虽然我们不相信它是有意的；如果大模型很聪明，教它干活既没有必要，甚至让它降智，降到“设计者的水平”， harness也变得有点臃肿，成为永远不会触发的代码。

这是第一个设计权衡：提示词和Harness。你的设计理念是就低还是就高，就低你需要做提示词和Harness的完整框架，能用且安全，但更多依赖开发者的认知，脆弱还降智；就高，你围绕着理想大模型去设计，相信大模型自己的能力，提示词和Harness只是当前不得不打的补丁，框架简洁、强壮，既会有意外的惊喜，它的表现优于设计者本人，当然也会有意外，它可能会瞎干和蛮干。

这个权衡现在看并不那么悲观，开始有人觉得大模型变得越来越聪明，聪明到某些厂商不敢发布。有的发布了被政府立即叫停。新闻炒作和事实交织吧。有一点是确定的，大模型比单个人要聪明，虽然不是每件事情都超过特定的某个人。

我们选择了就高。

第二个设计权衡，也就是我们常说的上下文工程。虽然当前的顶级大模型达到了百万和两百万Token上下文，实际使用中，系统提示词、工具Schema，工具输出，以及会话历史增长很快，甚至几轮就会耗尽，更谈不上让智能体长程工作。

像人类一样，大模型理解、思考和推理是离不开上下文的。除非背后的技术框架发生根本性革命，上下文会增大，但它永远是个约束。人类是怎么做到的，我们有些理论和猜想，但总体看记忆和回忆还是个谜团。

围绕着上下文和记忆，研究者和开发者做了大量的工作。核心的问题怎么让大模型在解决问题时获得足够的上下文，又不突破限制。首先，大家达成共识的是，智能体需要用数据库或文件构造永久记忆。第一，和大模型的所有交互都保存到永久记忆中，不丢失；第二我们需要设计一个forget，从当前上下文移除对当前工作毫无用处的内容， 第三我们需要一个recall，从永久记忆中回想起以前与上下文相关的内容并加载到当前上下文。

上下文问题还直接经济相关，目前大多数计费是按照token来计费的。

第三个权衡：怎么让智能体既像人一样工作，又像机器一样工作。像人一样工作，意味着发挥了大模型的知识和能力，也要与它的本质上不可靠共存。你安排一个工作给秘书？什么时候她能完全不折不扣的完成？几乎没有。智能体也一样，你告诉它用百度搜索一条信息，它转了一圈回来，说我搜索到了。但它可能用了别的搜索，像人一样“偷懒”。这是人类的智能问题，也是大模型的智能问题。

很多场景，我们还是希望它像机器一样工作，按照我们编排的流程去执行。这就是怎么设计确定执行的方案。当前有不少技术选项，最稳的还Coding，把一个流程编写为一个代码，按照代码的确定性逻辑执行。

5. 设计特点

- 有最新的、官方的、标准的、流行的package，一定要使用，不要重复造轮子。

- All are plugins design, plugin is a standard http streamable MCP with additional plugin contract。

- Sessionless：没有session概念，agent重启使用退出时的上下文。

- Turn-based：会话和持久化都是以turn为单位。

- Meta arguments: 所有Function tool都注入timeout, async， approve 参数，由agent运行时选择。

- Turn Prompt: 通过auto invoke _turn_prompt 工具，注入每轮loop所需要的附加提示词。

- Marker: 通过在原始user和assitant消息中追加Marker来注入需要大模型知道的信息。

- Channel: AgentLoop Inbox的来源， Inbox 进入AgentLoop有两个模式，排队和允许插队。插队是通过 auto invoke _check_new_input tool实现的， tool返回新的一条user message。 模式可配置。

- Silence Contract:大模型保持静默的契约是输出".".

- Heartbeat: 默认每30分钟，注入一条心跳Marker user消息。

- Schedule: 定时任务也采用用心跳机制。到触发时间，注入一条心跳任务的user消息。

- Multiagents: 多agent依赖mosquitto消息中间件，以A2A标准为蓝本实现。 完全异步。

- Progressive disclosure: 外部mcp 默认autoload=false, 使用渐进式披露，tool-search, tool-load

- 轻量级的job systems: job-coding plugin, 相对于native tools, 它可以用到外接mcp服务的全部能力，可以调用大模型做编排。

- 每个agent有大模型或人工可以修改的提示词部分USER.md，它相当于一个常驻记忆，反映用户使用偏好和要求。 启动是追加到系统提示词尾部，作为提示词的一部分。不应频繁修改破坏缓存命中。

- plugin, native tools and jobs are auto discorvered

- Installation: 一键安装：从源码安装，避免pypi库的版本冲突；自动安装所有依赖，开箱即用，但语义功能需独立安装和配置。

6. Context Harnessing

6.1 Tool Pair

_turn_prompt: Turn的提示词，每一轮开始，自动调用_turn_prompt， 用tool pair message注入到上下文，工具结果 turn_prompt.j2, 进入记忆。目的是让大模型在每轮开始知道更新的系统状态信息。 user → [attach_image pair] → _turn_prompt pair → LLM.

6.2 Context Marker

- slife 启动时，在每一个恢复的Turn的user message开头注入 [Turn:json] , 不进入记忆。目的是让agent知道上下文中每个Turn的id。
- slife 当上下文达到80%上限。系统移除历史turns，使之降到20%（靠估算）。在assitant message尾部追加 [Turn: ... removed]，不进入记忆。目的是让大模型知道发生了截断，上下文中移除了多少Turns. trailing footnote. 

6.3 Channel and Markers

Channel 是指Agent Loop Inbox的来源，TUI是默认的、正常的channel。

- Channel Heartbeat：系统每隔1800秒（默认值），向Inbox注入 [Heartbeat] click user Message， agent loop 闲时注入，忙时跳过。TUI 过滤这条User message，在状态栏提示；如果assistant message = '.', TUI过滤掉，非'.' 显示 自主 信息。进入记忆。使用silent handler，TUI过滤中间过程。

- Channel Subagent：只有当创建的subagent是自动推送结果时才会出现。自动推送的结果要加上[Subagent:json], json数据中要有suabgent name和task name（id），让大模型知道是哪个subagent的哪个task发过来的信息。TUI显示 Subagent(subagent name)> ，并过滤TUIMarker。

- Channel Wechat: 当用户微信输入时，注入inbox时加上 [Wechat:json], json里面包含send wechat message所需要的信息。TUI显示 Wechat> ， 并过滤掉marker。

- Channel A2A: 有两者情况， 

一种是发送消息和发送任务，需要在消息文本中增加Marker [A2A:json]，前者的json含peer，后者的json含peer，task。接收方 TUI 显示 A2A(peer)> ，TUI中过滤Marker。让大模型知道是哪个peer发过来的，如果是task，是什么task。

另一个是结果自动推送。自动推送中加入MARKER [A2A-PUSH:json], json与前面一样。接收方逻辑也一样。

- Schedule: 

定时任务触发执行注入 schedule_trigger.j2，没有Marker，TUI 过滤这条user message， assistant message 显示 定时。

定时任务结束的回复，是Subagent的回复, 按照Subagent channel 方式。

7. Design Points  

- A2A over MQTT: 集成a2a-over-mqtt标准库。支持task resquest, task response, message 和 broadcast消息类型，异步通信。

- Timeout，集中配置timeout，分类管理。原则只在阻塞点配置timeout，不配置timeout总量。唯一例外是在AgentLoop的工具执行中，配置了统一的timeout，避免tool 执行阻塞。同时允许agent选择配置工具执行的timeout。规则如下：
    1、工具执行timeout override 工具自身timeout: 如果agent没有选择配置timemout，则工具执行timeout生效，如果tool本身有timeout参数，则用工具执行timeout值赋值工具自身的timeout参数，使其自洽；否则工具执行timout兜底。
    2、agent选择配置的timemout override all：agent的timeout替代统一配置的工具执行timeout，并规则1处理后续。

- Async：允许大模型选择工具异步执行。选择了异步，系统先判断用户有没有选择approve，如果有先执行approve会话。 agent设了async，没设timeout， 就异步执行tool，不用看tool有没有timeout参数；如果slife 设了async，并同时设了timout，则看一下tool有没有timout参数，有的话用agent的timeout去赋值，没有话给异步执行加上timeout约束。

- Subagent：我们的设计是deliberately opionated。一个独立进程的headless agent，一个worker，没有人格，既可以空上下文执行，也可以fork agent的上下文，拥有主agent的所有能力。一个task是一个子agent的一个turn，可以同步也可以异步，没有持久化，没有错误处理。所以task的结果推送是harness的，而不是子agent使用工具推送回来。另外， subagent 也执行 _turn_prompt 和 trim， 无用也无害。

8. To do list

8.1 启动时（用户手改了大模型设置）或更换大模型时，上下文窗口可能变大或变小，怎么处理？

8.2 forget 和 recall：目前forget只有两个简单机制，大模型调用的clear context，另一个是harness调用的trim。recall是一个混合检索，以工具结果的形式注入到上下文。是否可以有个工具，在内存中更新自己的上下文，包括系统提示词、消息历史、工具列表。当大模型觉得当前的任务需要重新整理一下上下文？下一个迭代生效。副作用是影响缓存命中。有效可能不经济。排查一下现在的recall工具是否排除当前上下文？

目前的状况比较棘手，每次重启恢复退出时的上下文，单调增长到

8.3 共享代码库？现在项目里有重复的functions，增大代码量和维护量，是否值得？

8.4 多wechat接入

8.5 Tool System 重构

存在的问题：

- tool search 只能搜索mcp tool, 不能搜索其它类型的tool
- mcp eager connect, ready耗时，对于不用的mcp，浪费资源
- tool loading 没有统一的机制，原生tool默认全部load，mcp tool只能以mcp为单位，粒度太大，会导致上下文中tool schemas爆炸。

重构的目标，统一Tool System，统一的search 和 load(针对需要load的)，对系统所有的function tools load的tool用阈值管理。动态load 和 unload，避免上下文中tool schema爆炸。

分析：
- 目前tool的类别包括，builin, mcp, job, rest-api, cli，skill 6种 tools
- rest-api 是用 mcp-openapi-proxy 实现，某种意义上一个rest api 对应一个mcp server.
- 所以builin tools, mcp tools, jobs, rest-api本质上都是function tools，都需要加载到agent loop的tool lists 中。
- skill 需要通过工具将SKILL.md 加载到上下文中 （当前的工具名叫 open skill）

重构方向：

AgentRegistry运行时用tools.db。tools.db中两张表：

- ServerRegistry , 它包含 category：MCP | REST-API，server name, description, status: CONNECTED | DISCONNECTED | ENABLED | DISABLED | ERROR

- AgentRegistry 用 tools.db实现，它包含以下字段：name，description, category, source_kind, source_id, usage,  last loaded:

name ： 工具名，来自mcp的工具带 <mcp_server>__前缀, required。
description: required
category：Builin | Job | MCP | REST-API | SKILL | CLI
source：null | null | null | <mcp-server> | <mcp-server> | null | null
schema: Tool def(name,description, schema)|Tool def|Tool def|Tool def|SKILL.md|null

status: loaded | unloaded | null  （**存储列**，只有这三个值）
loaded unloaded只针对function tool，即 builtin, plugin, mcp, job, rest-api，对于skill和cli都为null

effective status: loaded | unloaded | error | disabled | n/a  （**推导值，不落库**）
由行上的三个事实按序推导，后者不覆盖前者：
  1. enabled == 0        → disabled （tools.json5 的开关；对每个category都适用）
  2. unavailable == 1    → error    （运行时判决：拥有者此刻不可用。**独立列**，
                                      绝不写进 status —— 写进去会把 model 的
                                      loaded 决定抹掉，掉线一次就丢）
  3. status              → loaded / unloaded，为空则 n/a

unavailable: 1 | NULL   — 拥有者（server / plugin）此刻不可用，由 host 写，清掉时不动 status

last-loaded: <Time> | null
只针对function tool。

emddings over usage, hybrid search

系统元工具， 属于builtin，始终loaded， 不可配置和更改。

- Search mcp:

1 mcp-search， by category， by scope: FTSS 搜索， name and description.
category 为空时， 所有category;默认值为空，category mcp 或 rest-api
status：为空时，搜索所有状态的， scope=NOT_CONNECTED, 只搜索status非Connected, scope的默认值为 NOT_CONNECTED。

- Connect 和 disconnet mcp/restapi 

2 mcp connect
3 rest-api connect
4 mcp disconnect
5 rest-api disconnect

- Enable 和 Disable mcp/rest-api

6 mcp enable/disable
7 rest-api enable/disable

- Search tool

8 tool search： hybrid search, 参数category和scope.
category 为空时， 所有category;默认值为空.
status：为空时，搜索所有状态的， status=status, 只搜索status的tool, 默认值是unloaded.

9 tool load：if not already loaded, - only Builtin，Job， MCP， REST-API， inject to llm tool list.

10 skill load: 将SKILL.md加载到工具输出中。

运行时逻辑

- 系统重启，只eager connect 状态为CONNECTED的mcp server和restpai server， 并在需要时更新 tools.db 的 TOOLRegistry, 1）新工具则增加 2）没有的删除 3）description变化的，更新 description，更新schema。连接失败，更新 server 和 tool 状态为error. schema更新了就需要重新embedding.

每轮Agent Loop重建上下文时，从AgentRegistry中取状态为loaded 工具，和schema，注入到tool list.

- 设置最大loaded function tool的阈值，动态管理，配置到tools.json5, 默认值为100

创建harness 工具， _unload_function_tool

每一轮当loaded 工具数超过阈值，就淘汰到阈值：`loop._maybe_evict` → `ToolCatalogService.evict_to_threshold`
→ `CatalogStore.evict_lru`（按 last_loaded 升序、NULL 最先，一次批量 UPDATE status='unloaded'；保护
ALWAYS_LOADED ∪ autoload ∪ autoload server 的全部工具）。这是 status 的第三个写点（另两个是
func-tool-load / _unload_func_tool），模型侧仍然只有那两个工具能动 load 状态。

淘汰只动 status，不碰 server：**宿主侧没有"服务器的连接"这个东西**。连接由 mcp-gateway 进程的连接池自己
持有与回收，宿主对 server 只有两个事实 —— json5 的 enabled 开关，和网关 __check 给出的判决（记在
unavailable 列）。所以"没有 loaded tool 就断连它"不是一条待实现的规则：既没有 connect/disconnect 这个动作，
也不该由 load 状态去驱动连接生命周期（load 是 model 的决定，连接是网关的事）。

- 为builtin function tool 配置preload，tool search 和 tool load必须配置为true，其它工具可以用户自配置。

9. 同步逻辑

原则：

1 tools.json5 是唯一真相，tools.db 是tools.json5的运行时镜像，它与 tools.json5的主要区别：

1）加了 type 数据（func | skill | cli），它是 category 的**派生投影**，不由 json5 配置，全库只有 reconcile 一个写入点。
2）增加了运行态列：status（loaded/unloaded，skill/cli 为 NULL）、last_loaded（LRU 排序），以及两列 json5 里也没有的：
   unavailable（拥有者此刻不可用的判决，独立列）和 enabled 的三态镜像（NULL=不表态，视为开启）。
3）mcp server / rest-api server 扩展成 tools 进入 db：命名 `{server}__{tool}`，source_id 指 server，
   v2 起**没有 server 表**（该表已 drop），server 的状态就记在它自己那些 tool 行上。
   注意 source_id 不等于"外部 server"：plugin/job 行也带 source_id，但网关子进程死亡时**不得**
   把它们标成不可用，所以 SERVER_CATEGORIES 只含 mcp / rest-api。

tools.db 里唯一 json5 无法重建的东西是 status + last_loaded（model 的决定）；其余每一行都是派生数据，
所以 schema 变了就删库重建，不做原地升级（旧文件由 _check_categories / _check_columns 报出来）。

2 cli的同步机制：

- 忽略 autoload
- 检查json5与

原则 tools.json5 是唯一真相， tools.db 是真相的扩展版（增加了mcp和restapi连接后的工具）。

1、启动时 

skill: 

1、自动发现skill， skill按skill规范解析，记下解析错误，让system health 能报告它。如果有错误，但解析到了名字，而这个名字在tools db中存在，则update这条记录的status 为error.



, 如果tools.db 中没有，则add

1、启动时连接所有 enabled servers和restapi servers, 把它们的工具同步到tools.db
    1.1 同步办法：对于每一个 server， 新工具db add, server中不存在的工具，db delete, 
    存在，但tool有变更，db update， 不update tool的状态 loaded unloaded，但要把disabled update为enabled。

    实现位置：_upsert_external_catalog_rows（host）——一次 upsert 该 server 的**全部**工具，
    再用 purge_source_except(server, incoming) 删掉"库里有、这次列表里没有"的行（注册表侧
    早已同样摘掉 proxy）。安全前提：空列表一律提前返回（"还没就绪"，不是"一个工具都没有"），
    所以禁用/未连上的 server 不会被清空。同一条"消失的工具必须丢行"的约定，plugin/job 由
    sync_system_tools(source=…) 提供，skill/cli 由 sync_category(purge=True) 提供。




2、动态enable和disable更新 只更新tool.db中的enable和disable属性值，不删除disable的行


 要确定启动时，和所有crud操作 mcp， skill， restapi， cli， job， 能同步到 tools.json5, 和 tools.db

唯一例外， 动态enable和disable只更新tool.db中的enable和disable属性值，不删除disable的行。

（早期写法"启动时只同步 enable tool 到 tools.db"对本地家族已不成立：config 关掉的 builtin 也会拿到一行并标
disabled，这样 json5 和 db 对同一个工具的说法一致。对 mcp/rest-api，disable 的 server 不连接，所以"没有行"
只发生在**从未连上过**的情况；曾经连上过的 server 被 disable，行保留并标 disabled —— 这正是上面那条
"不删除 disable 的行"约定。disable ≠ error：前者是 json5 的开关，后者是"开着但此刻连不上"。）
### 同步六原则（逐条对代码核对，2026-09-17）

1. **schema 是新值或发生变化 → embed / re-embed**。判据是 flatten 后的文本（`_flatten_schema`）：新行只有
   "可嵌入 schema"（非空）才失效；已有行 raw schema 变了、flatten 结果没变（enum/default/pattern/format、
   一层以上嵌套）就不重嵌 —— 否则会删掉向量再嵌回一条字节相同的向量。落点在 `CatalogStore.reconcile`，
   失效即删向量，drainer 依 `count_unembedded` 重嵌；非 mcp 路径要 `wake_indexer` 唤醒 drainer。
2. **启动时 config 的 enable 值 override db 的 enable 值 —— 值不同才 override**。本地家族经 `_row_enabled`
   + reconcile 的逐列比较；mcp/rest-api 经 `set_source_enabled`（也只写真会变的行）。NULL 算"不同"：
   列上的 NULL 是"不表态"（读作开启），把开关的显式值写上去正是 config-wins 本身。
3. **所有工具同步都判断相同时跳过，不同才 update**。四个家族汇流到同一个 `reconcile`（内存比较、只拼变化的
   SET，稳态启动一行不写）。三列 verdict 同理（`mark_source_unavailable` / `mark_all_external_unavailable` /
   `clear_source_unavailable` 都只写会变的行）：任何 UPDATE 都触发 `tool_au` 把该行重新索引进 `tool_fts`，
   白写就是白 churn。
4. **永不 update db 的 status**。写点只有三个：`func-tool-load`（`load_tool`，含 materialize 失败的回滚）、
   `_unload_func_tool`（`unload_tool`）、harness 的阈值 LRU（`evict_lru`）。`reconcile` 只在 INSERT 时落
   status（已有行保留 model 的决定）；拥有者不可用的判决走独立的 `unavailable` 列，绝不写进 status。
5. **db remove 掉 json5 中不再配置的 tool**。对 mcp / rest-api server，整源批量 `purge_source`
   （`purge_unconfigured_sources`：启动一次 + 每轮 reconcile 一次）；server 还在但少发布一个工具走
   `purge_source_except`（upsert-then-purge，空列表提前返回 = "还没就绪"）。plugin/job 走 source 限定的
   `sync_system_tools`，skill/cli 走 `sync_category(purge=True)`。
6. **所有 tool 的 set**：新工具 db add 默认 enabled（config 对它有表态就按 config —— config 里 disable 的
   builtin 也是这样拿到一行标 disabled 的）；已有工具的 enable 只在 config 表态且与 db 不同时才 override
   （同 2），config 不表态（`enabled=None`）就整行不写。CRUD 的 update 路径不得改变 enable —— `cli.py`
   更新条目时保留 `old_enabled` 即此例。
