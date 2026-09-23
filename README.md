# ReflexMesh

Интеграционный проект маршрутизации и выполнения задач для AI-агентов.
Текущий инкремент: **V0.1 — строгие routing-контракты и CLI с явной заглушкой**.
Jev, браузерное выполнение, MCP и Pi пока не подключены.

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

Коды выхода: `0` — маршрут выбран, `3` — нет допустимого маршрута,
`2` — ошибка ввода. Результат — JSON в stdout, ошибка — JSON в stderr.

## Проверки

```sh
python -m unittest discover -s tests -v
```

## Документы

- [Архитектурные инварианты](docs/architecture/INVARIANTS.md)
- [Решения по ревью инвариантов](docs/architecture/ADR-0001-invariant-review.md)
- [Спецификация V0.1](docs/specs/V0.1.md)

V0.2: реальная маршрутизация Jev. V1.0: ограниченное браузерное выполнение
с внешней проверкой результата. V2.0: ориентир полной первой концепции.
Оставшиеся пустые модули — заготовки, не реализованные возможности.

Проверки текущего инкремента: [отчёт V0.1](docs/research/V0.1-validation.md).
