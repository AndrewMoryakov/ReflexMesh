# Текущее состояние ReflexMesh

Последняя статическая сверка: 2026-10-12, кодовая база PR #10
[4a8385f](https://github.com/AndrewMoryakov/ReflexMesh/commit/4a8385f08b15ecdbdf96ad262f95c3673f94637e).
Общий браузерный путь, fixture Task API/MCP и Pi-пакет присутствуют в этой
проверяемой ветке. Это не утверждение о слиянии PR или закрытой приёмке.
Сверка и сопутствующие исправления review проверены только чтением исходников;
новых тестовых, браузерных или модельных прогонов не выполнялось.
Предыдущая статическая сверка: 2026-10-11, кодовая база
[b17813b](https://github.com/AndrewMoryakov/ReflexMesh/commit/b17813b25f8b6086c35d8220c461df43518bed53).
Новых тестовых, браузерных или модельных прогонов при этой сверке не выполнялось.
Прогоны общего браузерного пути V0.6 (M1–M2) от 2026-10-11 сделаны на локальной интеграционной ветке
`integrate/v0.6-into-main` (коммит `e5187be`; на дату отчёта ветка не была отправлена
и не была слита в `main`; CLI-свидетельство записано на `fc765d6`); подробности и оговорки —
[отчёт M1–M2](research/V0.6-M1-M2-2026-10-11.md). Остальные утверждения этого файла относятся к своей дате и кодовой базе.
Историческая проверка routing от 2026-09-24 использовала ранний контрактный baseline
[62ccf0d](https://github.com/AndrewMoryakov/ReflexMesh/commit/62ccf0d29c5d0e599f92561b4d8da3f980d63719).
Последующий live-прогон и расширенные сценарии имеют собственные commit и среду в отчётах.
Live gate V0.2 закрыт 2026-09-24 для `openrouter:~typesafe/jev-latest` на коммите 7c03412 ([отчёт](research/V0.2-live-openrouter.md)).

Концепция уточнена: ReflexMesh — универсальное ядро решений и координации,
браузер — первый execution-сценарий V1.0, desktop — последующее расширение.
Текущий приоритет разработки — M1–M2 [V0.6 Browser Core](specs/V0.6-browser-core.md):
общий DOM-адаптер и реальный выбор действий Jev. Краткий ориентир —
[CURRENT_DIRECTION](CURRENT_DIRECTION.md); открытая приёмка fixture V0.5
сохраняется и не подменяет этот приоритет.
Прямые API/MCP tools, CLI/скрипты
и другие исполнители обозначены как целевые направления. Эти классы исполнителей не реализованы
и не включены дополнительно в V1.0. Текущий CUA/LLM/PERCEPTION — ограниченный
экспериментальный каталог, а не окончательная классификация проекта.

## Реализация и имеющиеся свидетельства

| Область | Реализация | Свидетельства / ограничения |
|---|---|---|
| V0.1 | Task 0.1, stub RoutingDecision 0.1, CLI, валидация | [Отчёт V0.1](research/V0.1-validation.md): 10 тестов, включая 64 сочетания маршрутов |
| V0.2 | HTTP-клиент JevRouter; решение 0.2; фильтрация, отказы и trace | [Отчёт V0.2](research/V0.2-validation.md): всего 21 тест; настоящий upstream server в demo |
| V0.2 live | ReflexMesh → JevRouter → OpenRouter → `typesafe/jev-1.13-20260917` | [Live-отчёт](research/V0.2-live-openrouter.md): 5/5 пунктов pass, вызовы подтверждены ledger OpenRouter; typesafe напрямую не проверен. [Расширенные сценарии](research/V0.2-live-extended-openrouter.md): все три маршрута, RU/EN, пары, 5 повторов, live-ошибка провайдера |
| Upstream | JevRouter на закреплённом commit f944acb | Проверено HTTP/contract и live через OpenRouter |
| V0.4 | SystemOneHarness + Browser Use под контролем runtime на фикстуре | [Исторический spike](research/V0.4-spike.md): 18/18 детерминированных и 12/12 Jev-прогонов; ограниченный стенд и среда |
| V0.5, экспериментально | `reflexmesh run`, supervisor/worker, отмена/dispatch, лимиты, TextSlots, SOH-адаптер и fixture verifier реализованы в репозитории | Исторически записано U 36/36; исходный SHA `1887114` недоступен. [Отдельное ревью](research/V0.5-review-2026-09-25.md) сообщает воспроизведение U на `fe088e6` и один Chromium smoke, не зачтённый в B. [Оговорки о происхождении и покрытии](research/V0.5-U-acceptance.md#static-review-addendum-2026-10-07); B/J acceptance открыт |
| Поздние fixture изменения, статически | Обязательный completion gate, закрытые свидетельства передачи TextSlots, атомарный claim фикстуры, привязка сессии и terminal fencing есть в текущем коде | [Completion/slots](guides/V0.5-local-run.md#completion-assessment-gate), [fixture ownership](guides/V0.5-local-run.md#controlled-fixture-claims-and-attempt-bound-results); это не новые выполненные регрессии и не полная приёмка |
| V0.6 Browser Core | В проверяемой ветке есть `browser-task/0.1`, общий DOM-адаптер, провайдеры решений, контролируемый прогон, `reflexmesh browser run` и read-only `reflexmesh browser view` ([руководство](guides/V0.6-browser-run.md)) | [Отчёт M1–M2](research/V0.6-M1-M2-2026-10-11.md): M1 — сценарий на реальном Chromium (script-провайдер, сайт без разметки ReflexMesh) и отказ от устаревшего узла проверены; M2 — механика (допуск, TextSlots, бюджеты, трасса) проверена на script и на заглушке эндпоинта; **настоящий Jev не запускался** (нет ключа); `completed` недостижим без verifier (M4); handoff-продолжения (M3) нет; 63 новых теста, модуль с браузером 21/21 в трёх прогонах подряд; отдельно (2026-10-12) read-only страница хода прогона `reflexmesh browser view` с 14 тестами. Эти сведения сохраняют свои даты и исходную кодовую базу, а не подтверждают текущий head. **2026-10-13:** [первые прогоны с настоящим Jev](research/V0.6-jev-run-2026-10-13.md) (`typesafe/jev-1.13-20260917` через OpenRouter) на демо-задаче: Jev сам выбрал операции, цели и слоты, путь пройден; в первом прогоне заметка сохранена 9 раз — проверка повтора изменяющего действия была привязана к состоянию страницы, исправлено (`repeated_mutation`), во втором прогоне одна заметка и `needs_handoff/no_progress`; `completed` по-прежнему недостижим (M4). Тогда же подключена [локальная laya-browser](research/V0.6-laya-local-provider-2026-10-13.md) через переходник — дополнительный провайдер, для нужд ReflexMesh недостаточен. **2026-10-13, M4a:** [общий проверяющий страницы](research/V0.6-M4a-verifier-2026-10-13.md) — `page_match`, `scoped_extract`, `revisit_match` проверяются runtime на каждом наблюдении, со структурной привязкой и повторным открытием документа через шлюз; на демо-задаче (ревизия 2) критерий `pass` и со script, и с Jev, но итог `incomplete/verification_unknown`: `ownership`, `no_uncertain_repeats` и эффекты кликов не установлены. **2026-10-13, M4b–M4c:** [первый `completed`](research/V0.6-M4bc-completed-2026-10-13.md) — реестр зарегистрированных проверок (`--checks`, вид `order_desk` — оракул только демо-сайта) закрывает эффекты и повторы, владение браузерной сессией подтверждается в области «сессия, запущенная попыткой»; на демо-задаче `order-note-verified` `completed/verified` и со script, и с Jev. На сайте без зарегистрированного источника `completed` недостижим. **2026-10-13, M3:** [handoff в той же сессии](research/V0.6-M3-handoff-2026-10-13.md) — `BrowserCore` (embedding): сессия удерживается под lease с одноразовым токеном, новая попытка с новым owner epoch и общим бюджетом, режимы `jev` (новая ревизия) и `llm` (до пяти шагов из конечных вариантов); недостающее значение с настоящим Jev разрешено новой ревизией в том же браузере до `completed`, неоднозначность — только детерминированно. **2026-10-13:** [сервис сессий](research/V0.6-session-service-2026-10-13.md) `reflexmesh browser serve` (Unix-сокет 0600, `submit`/`resume`/`cancel`/`status`/`shutdown`) — продолжение из CLI; цепочка с настоящим Jev и одним шагом помощи дошла через него до `completed`; одноразовый `browser run` продолжения по-прежнему не предлагает. **2026-10-13:** [браузерный путь на нативном Windows](research/V0.6-windows-2026-10-13.md) (`browser run`, `serve` через именованный канал с ключом, `view`); проверен на Windows 11 сценарием и тестами, настоящий Jev с Windows не запускался (openrouter.ai оттуда отвечает 403 без VPN). Готовность на произвольных сайтах не подтверждена |
| Fixture Task API / MCP / Pi, экспериментально | `src/reflexmesh/api` содержит TaskService и stdio MCP с `reflexmesh_submit`, `reflexmesh_status`, `reflexmesh_cancel`, `reflexmesh_continue`; `integrations/pi` содержит extension и skill ([руководство](guides/V0.6-mcp-pi.md), [контракт Task API](specs/V0.6.md)) | Принимается `execution-task/0.1`; продолжение создаёт связанную попытку со свежим браузером и остатком бюджета. Это не общий `browser-task/0.1` API и не M3 в той же сессии. Наличие кода и acceptance runners не закрывает приёмку MCP/Pi или V1.0; свежих результатов для текущего head нет |
| Платформы V0.1/V0.2 routing | Linux/Python 3.12.14/Node 24.19.0; Windows 11/Python 3.13.15/Node 24.15.0 | В историческом прогоне 25 тестов прошли на обеих платформах (Windows-специфичный timeout в тесте исправлен в 599b58d) |

Числа тестов и конфигурации относятся к указанным в отчётах прогонам;
актуализация документации сама по себе не является новым runtime-прогоном.
Stub остаётся провайдером по умолчанию. `--provider jevrouter` — явное включение
HTTP-пути; выбранный маршрут никогда не означает выполненную пользовательскую задачу.

## Пока отсутствует

- Остальное общее браузерное ядро V0.6 (код M1 и механики M2 есть в проверяемой ветке): запуск с настоящим Jev,
  общие predicates и verifier, доказательства `ownership` и `no_uncertain_repeats`, предотвращение (а не обнаружение)
  ухода на чужой origin, пробы frames/shadow DOM, press_key/focus_region.
- Handoff и продолжение LLM/Jev в той же живой сессии через SessionManager/TaskChain;
  текущий fixture HarnessStrategy закрывает environment после своего segment.
- Live-подтверждение прямого провайдера typesafe.
- Завершённая приёмка V0.5. Экспериментальный runtime уже есть в коде, но B/J-матрицы
  с необходимыми повторами не закрыты. Исторический U-результат не подтверждает
  полное покрытие: C12 проверяет очистку логов только при `executor_unavailable`,
  до браузерных действий, и пропускается при установленном Browser Use.
  См. [дополнение к U-отчёту](research/V0.5-U-acceptance.md#static-review-addendum-2026-10-07).
- Cua and desktop actions; recovery and a durable execution receipts store.
- MCP-интерфейс общего Browser Core (`browser_task`, `browser_resume`, `browser_cancel`)
  и его Pi-путь; существующий fixture MCP/Pi их не реализует. HTTP Task API.
- OCR/VLM, ClawBridge, Skyvern, Ui.Vision, Jev meso-supervisor.

Оставшиеся пустые модули/каталоги являются каркасом; это не относится к
`src/reflexmesh/api`, `runtime/handoff.py` и Pi-пакету. Наличие stdout trace
в V0.2 не равно полноценному audit trail задач; upstream хранит собственные receipts.

## Текущая работа и прежние открытые gates

Следующая разработка — доведение и подтверждение M1–M2 по [ROADMAP](ROADMAP.md),
включая настоящий Jev; существование fixture MCP/Pi этот порядок не меняет. При переиспользовании
runtime проверяются и исправляются необходимые control/evidence gaps.
Полная fixture приёмка V0.5 остаётся открытой; ни её старый U-результат,
ни новый план не дают права объявлять browser core готовым. Подробные
условия M1–M5 и новой приёмки задаёт V0.6.

Ниже — состояние ранее исследованных путей, а не очередь следующих задач.

Live gate V0.2 закрыт для OpenRouter ([отчёт](research/V0.2-live-openrouter.md));
typesafe требует отдельного прогона. Подробности — [V0.2](specs/V0.2.md).
V0.3 выполнен по [утверждённому протоколу](specs/V0.3.md): вердикт **useful**
([отчёт](research/V0.3-routing.md)). Jev: accuracy 1.00 (как gpt-5.5), rules 0.65
(на непредусмотренных формулировках 0.13), p50 0.6 с против 6.4 с, ≈ $0.00002 против ≈ $0.002
за решение. Слабое место — смысловой отказ: Jev выбирает ближайший разрешённый маршрут
(корректный отказ 25% против 79% у LLM). [V0.3b](research/V0.3b-refusal.md): кандидат `NONE`
поднимает корректный отказ Jev до 96% без потерь на обычных задачах; реализовано в адаптере
([ADR-0003](architecture/ADR-0003-none-candidate-refusal.md), [live-проверка](research/ADR-0003-live-check.md)). V0.5 остаётся открытым прежним acceptance gate.
Правила INV-03 уточнены через [ADR-0002](architecture/ADR-0002-routing-effects-and-loop-control.md).
Эта статическая актуализация не добавляет новых execution-свидетельств.
Исторические V0.4-прогоны и V0.5 smoke описаны отдельно; smoke не закрывает B/J.

## Известные границы реализации

- capabilities/allowed_routes — объявления и routing-фильтр, не проверка доступности исполнителей или полноценная авторизация.
- Адаптер routing делает исходящий HTTP к loopback JevRouter без proxy/redirect/retry.
  Есть локальный stdio MCP Task API над fixture `run`; HTTP Task API отсутствует.
  Экспериментальный `run` дополнительно использует браузер, HTTP-фикстуру и выбранный action provider.
- Socket I/O timeout адаптера routing не является общим wall-clock deadline. У `run` есть отдельный deadline супервизора; полное соответствие V0.5/INV-10 остаётся открытым.
- Runtime V0.5 использует POSIX `fork` и требует Linux/WSL; исторические Windows-проверки routing не подтверждают поддержку `run` на native Windows.
- Пять постусловий verifier используют состояние/события сервера: `settings_saved`,
  `form_submitted_once`, `export_completed_once`, `account_intact`, `support_request_sent_once`.
  Сопутствующее source-исправление review добавляет связанный с попыткой `/support`:
  один POST и прирост счётчика на один, имя/email из указанных слотов и непустое
  сообщение, равное одному из принятых слотов. Это свидетельство отправки,
  не доказательство конкретного вызова ввода; новое выполнение проверки не заявляется.
  Для `current_page` нужно соответствующее наблюдение браузера с текущим URL и целью;
  исторического GET недостаточно.
- Completion gate требует `pass` всех шести обязательных execution constraints,
  успешных постусловий и определённости эффектов. `runtime.budget` оценивает
  локальные счётчики; `runtime.immutable_slots` — точный payload, переданный
  поддерживаемому драйверу, при полном закрытом наборе свидетельств. Потеря
  свидетельств даёт unknown, доказанная подмена текста — sticky fail. Это
  не доказывает фокус, итоговый DOM или внутреннее поведение Browser Use.
- Атомарный fixture claim, server-bound эффекты, привязка private profile/session
  и terminal fencing имеют ограниченную область. `runtime.ownership` может
  фиксировать доказанную смену identity как sticky fail, но не получает pass:
  исключительный контроль браузера/OS не доказан. Неопределённая очистка оставляет
  фикстуру в quarantine. См. [область гарантии](guides/V0.5-local-run.md#controlled-fixture-claims-and-attempt-bound-results).
- В текущем коде fixture оценки `runtime.permissions`, `runtime.dispatch_ordering`
  и `runtime.no_uncertain_repeats` могут получать pass при полном наборе
  свидетельств gate и сервера. Это статическое свойство реализации, не результат
  нового прогона; исторические unknown не переписываются. Неизвестное ownership
  или любое другое обязательное unknown по-прежнему исключает completed.
  Поздние slot/ownership и review-исправления не проверялись новым прогоном в этой сверке.
- Реальные selector-map/node-identity пробы и smoke описаны в [ревью V0.5](research/V0.5-review-2026-09-25.md). Идентичность документа/фрейма, enabled, назначение формы и ревизия политики остаются неполными; приёмочные B/J-повторы ещё нужны.
- `is_stub=false` и provenance от локального сервиса не доказывают обращение к модели.
- `needs_confirmation` возвращает предложение; механизма подтверждения и последующего исполнения пока нет.
- Отказ, ошибка и выбранный маршрут имеют разные исходы; автоматического fallback на stub нет.

### Общий браузерный путь (код проверяемой ветки)

- Запускается `reflexmesh browser run` на Linux/WSL; цикл идёт в вызывающем процессе с потоком-сторожем, а не в
  изолированном supervisor-процессе. Chromium может потребовать `--no-sandbox` (результат это фиксирует).
- Строки `runtime.permissions`, `dispatch_ordering`, `immutable_slots`, `budget` получают pass по свидетельствам
  шлюза и реестра слотов; `ownership` и `no_uncertain_repeats` остаются `unknown`. Критерии задачи — `unknown`
  (verifier M4 нет), поэтому попытка не бывает `completed`.
  С 2026-10-13 критерии `page_match`/`scoped_extract`/`revisit_match` проверяются (M4a) и могут получить pass/fail;
  попытка по-прежнему не бывает `completed`, пока `ownership`, `no_uncertain_repeats` и эффекты не установлены.
  С 2026-10-13 (M4b–M4c) `ownership` получает pass в области «сессия, запущенная попыткой» (чужой клиент на
  отладочном порту — fail), а `no_uncertain_repeats` и эффекты — только по зарегистрированному источнику (`--checks`);
  `completed` достижим лишь при нём, сейчас — только на демо-сайте.
- Эффект клика и ввода на сервере остаётся `unknown`; интерфейсное наблюдение (`ui_effect`) — не серверный эффект.
- Изменяющее действие, уже отправленное на том же документе (URL), повторно не отправляется (`repeated_mutation`),
  как бы ни изменилась страница; то же действие в неизменном состоянии — `repeated_action`; три цикла без нового
  состояния — `needs_handoff/no_progress` (с 2026-10-13, после прогона Jev с девятью сохранениями).
- Редирект на чужой origin обнаруживается после запроса (`blocked/policy_denied`), не предотвращается.
- Возможности `press_key`, `focus_region`, `frames`, `cross_origin_frames`, `open_shadow_dom` объявлены `false`, а не проверены пробами.
- Fixture оценки и общий Browser Core имеют разную область доказательства:
  серверные fixture свидетельства не подтверждают constraints произвольного сайта.

## Покрытие инвариантов

| INV | Текущий уровень |
|---|---|
| 01, 03, 07 | CLI/routing работают без Pi; routing отделён от экспериментального браузерного `run`, общий исполнитель задач отсутствует |
| 04, 13 | Routing-фильтрация и проверки ответа; runtime дополнительно проверяет fixture permissions, но полный adapter/policy contract не подтверждён |
| 06, 08–11 | В коде есть TextSlots, обязательный completion gate, dispatch/cancel/budget guards и scoped fixture ownership; это частичные механизмы, не доказательство полного соответствия. Обычный путь сохраняет unknown constraints |
| 14 | Routing-трасса и экспериментальные `result.json`, `trace.jsonl`, `evidence.jsonl`; полный audit contract и очистка логов обычного исполнения не подтверждены |
| 16 | Routing измерен в V0.3/V0.3b; есть ограниченные V0.4 browser-прогоны и V0.5 smoke. Приёмочная матрица качества выполнения V0.5 не завершена, стоимость модели не измерена |
| 02, 05, 12, 15 | Требования к целевой системе; соответствие execution-путей не подтверждено |

## Аудит документации

Вход для работающего агента — [AGENTS](../AGENTS.md) и
[CURRENT_DIRECTION](CURRENT_DIRECTION.md). [Карта документов](README.md)
разделяет текущие требования, факты реализации и датированные свидетельства.
Исторические отчёты сохраняются; их старые рекомендации не переопределяют
текущий порядок разработки.

Обновляйте этот файл при изменении состояния этапа: дата, кодовая база,
что реализовано, какой проверкой подтверждено и какой gate ещё открыт.
