# ReflexMesh

Интеграционный проект маршрутизации и выполнения задач для AI-агентов.
Текущий инкремент: **V0.2 — адаптер локального JevRouter и CLI**.
HTTP-интеграция проверена с настоящим сервером в demo-режиме; облачная модель
пока не проверена. Браузерное выполнение, MCP и Pi ещё не подключены.

## Запуск

Python 3.11+. Установка в виртуальное окружение (Bash):

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/reflexmesh route --input examples/route-task.json
```

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\reflexmesh.exe route --input examples/route-task.json
```


Без установки пакета (Bash):

```sh
PYTHONPATH=src python -m reflexmesh route --input examples/route-task.json
```

PowerShell без установки:

```powershell
$env:PYTHONPATH = "src"
python -m reflexmesh route --input examples/route-task.json
```

`--input -` читает JSON из stdin. Маршрут выбирается из пересечения
`capabilities` и `allowed_routes` в порядке CUA, LLM, PERCEPTION.
Заглушка не анализирует цель; поле `provider` равно `stub`, `is_stub=true`,
`execution_performed=false`, `confidence=null`. Объявление возможностей
не означает, что исполнители подключены. Результат не является выполнением задачи.

Коды выхода: `0` — маршрут выбран, `3` — отказ, `4` — требуется подтверждение,
`5` — ошибка адаптера/провайдера, `2` — ошибка ввода. Результат — JSON в stdout, ошибка — JSON в stderr.

## Проверки

```sh
python -m unittest discover -s tests -v
```

## Документы

- [Архитектурные инварианты](docs/architecture/INVARIANTS.md)
- [Решения по ревью инвариантов](docs/architecture/ADR-0001-invariant-review.md)
- [Спецификация V0.1](docs/specs/V0.1.md)

V0.2: live-прогон Jev ещё предстоит. V0.3: сравнительная оценка routing. V1.0: ограниченное браузерное выполнение
с внешней проверкой результата. V2.0: ориентир полной первой концепции.
Оставшиеся пустые модули — заготовки, не реализованные возможности.

Проверки текущего инкремента: [отчёт V0.1](docs/research/V0.1-validation.md).

## JevRouter (V0.2)

Заглушка по умолчанию сохранена; реальная интеграция включается явно.
Установите upstream отдельно (Node.js 20+, Git):

```sh
git clone https://github.com/BillionsBobby/JevRouter.git
cd JevRouter
git checkout f944acb6530621bced023352e2358a63218bf4d9
npm ci --ignore-scripts
npm run build
node dist/cli.js serve --provider typesafe --port 8787
```

Перед запуском сервера задайте в его окружении `TYPESAFE_API_KEY` или
`JEV_API_KEY`. Для OpenRouter задайте `OPENROUTER_API_KEY` и используйте
`--provider openrouter`. Ключи не передаются в Task или CLI ReflexMesh.
Сервер использует собственную policy и сохраняет receipts в `.jevrouter/`
своего рабочего каталога; учитывайте это при выборе каталога и содержимого задач.
При настоящем провайдере goal передаётся облачной модели.

Во втором терминале из каталога ReflexMesh, после установки:

```sh
python -m reflexmesh route --provider jevrouter --input examples/route-task.json
```

Доступны `--jev-url http://127.0.0.1:8787` и `--timeout 30` (socket I/O,
не общий deadline). При ошибке модель не заменяется заглушкой. При ответе
`needs_confirmation` дальнейшие действия не выполняются.

Без ключа можно запустить upstream с `--provider demo`, а клиент с
`--allow-demo`. Ответ будет помечен `is_stub=true`. Без этого флага demo отклоняется.
Пример с двумя маршрутами может дать честный отказ по низкой оценке.

Проверка интеграции без ключей после сборки upstream:

```sh
python scripts/check_jevrouter_http.py --upstream-dir /path/to/JevRouter
```

Скрипт сам запускает и завершает локальный demo server во временном каталоге.

- [Спецификация V0.2](docs/specs/V0.2.md)
- [Отчёт проверки V0.2](docs/research/V0.2-validation.md)
