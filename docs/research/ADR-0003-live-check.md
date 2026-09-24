# ADR-0003: live-проверка кандидата NONE в адаптере

Дата: 2026-09-24, 19:40–19:42 UTC. Код: `23f0a3ae712a9dbebf98fd07a4addc77ee44b094` (чистое дерево),
JevRouter `f944acb`, `openrouter:~typesafe/jev-latest`, Linux, прямой доступ к OpenRouter.
Решение: [ADR-0003](../architecture/ADR-0003-none-candidate-refusal.md).

**Итог: адаптер воспроизводит вариант J1 из V0.3b end-to-end через CLI; live-пакет V0.2 проходит.**

## Проверки

1. **Контракт:** 37 тестов. Запрос адаптера по умолчанию байт в байт равен запросу J1 из
   V0.3b, а с `--no-none-candidate` — J0. Выбор `NONE` даёт `abstained` /
   `upstream_no_fitting_route` и никогда не становится маршрутом.
2. **Demo upstream:** `scripts/check_jevrouter_http.py` — selected, low-confidence abstained,
   demo_guard.
3. **Live-пакет V0.2** (`collect_v02_live.py`, openrouter): `mechanical_checks_passed: true`.
   multi → LLM (P: CUA 0, LLM 1, NONE 0, PERCEPTION 0), filtered → LLM без CUA, empty → локальный
   отказ без запроса. Выбор при подходящем маршруте не изменился.
4. **Набор V0.3b через CLI** ([`scripts/check_none_e2e.py`](../../scripts/check_none_e2e.py), 84 кейса,
   1 проход):

| | adapter (CLI, e2e) | V0.3b J1 (harness, 3 прохода) |
|---|---|---|
| control accuracy (60) | 1.00 | 1.00 |
| ложный отказ на control | 0 | 0 |
| корректный отказ, всего (24) | 0.958 | 0.958 |
| — нужный маршрут запрещён | 1.00 | 1.00 |
| — вне каталога | 0.917 | 0.917 |
| пропуск | R2-O-02 (cron → LLM) | R2-O-02 |
| forbidden / ошибки | 0 / 0 | 0 / 0 |

Отказы: `upstream_no_fitting_route` 18, `upstream_no_decision` 5. Данные:
[`experiments/adr0003/e2e/`](../../experiments/adr0003/e2e/) (SHA-256 `decisions.jsonl` 0b577d5b…a200,
`report.json` 43f7298f…c42b).

## Ограничения

Один проход e2e; набор и описание `NONE` те же, что в V0.3b (не независимая проверка качества).
Более трудный набор остаётся открытым вопросом V0.3.
