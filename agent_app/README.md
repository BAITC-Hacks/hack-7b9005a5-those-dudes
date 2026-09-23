# HackAlem Agentic AI

Локальный объяснимый помощник аналитика поверх результатов «Графа денег».
Основной режим полностью детерминирован и не вызывает внешние API. Опциональный
режим OpenAI Agents SDK использует одного агента и те же read-only инструменты.

## Возможности

- профиль узла и объяснение `role_score` / `priority_score`;
- входящие и исходящие соседи;
- исходящие маршруты до четырёх рёбер;
- общие прямые получатели нескольких источников;
- сопоставление 2–10 узлов по одинаковому набору метрик;
- сводка кластера и приоритетные кандидаты;
- отдельный список неопределённых/depth-4 случаев;
- результаты экспериментов по устойчивости сети.

Каждый ответ содержит оговорку о неполной исходящей выборке. Помощник не
обогащает клиентов, не меняет файлы и не делает выводов о виновности.

## Запуск без API

Из корня репозитория после выполнения основного pipeline:

```powershell
python -m agent_app.main
python -m agent_app.main query "покажи топ 10 кандидатов"
python -m agent_app.main action node_profile --gid <GID>
python -m agent_app.main action neighbors --gid <GID> --direction out --limit 10
python -m agent_app.main action trace_routes --gid <GID> --max-hops 3
```

Если outputs ещё не рассчитаны, health и инструменты сырого графа продолжат
работать, а ролевые запросы честно вернут `unavailable`.

## Локальный HTTP API

```powershell
python -m agent_app.main serve --port 8770 --static-dir dashboard/dist
```

- `GET /health`
- `GET /api/capabilities`
- `POST /api/query` — `{"question":"объясни приоритет <GID>"}`
- `POST /api/action` — `{"action":"uncertain_nodes","limit":10}`

Общий чат `/api/query` и структурированные `/api/action` работают офлайн.
Кнопка в карточке узла вызывает отдельный `POST /api/explain-node` с `{"gid":"..."}`
и заголовком `X-HackAlem-Action: explain-node`. При наличии ключа она использует
OpenAI для объяснения сохранённой роли, без изменения результатов pipeline.
`GET /api/ai-status` показывает готовность и модель; значение ключа не возвращается.

## Опциональный Agents SDK

Установите optional dependency и настройте ключ только если это разрешено вашей
средой:

```powershell
python -m pip install -r requirements-ai.txt
# Заполните OPENAI_API_KEY в .env в корне проекта.
python -m agent_app.main query --mode sdk "Сравни два указанных узла"
```

Настройки `OPENAI_API_KEY`, `OPENAI_MODEL` и `OPENAI_TIMEOUT_SECONDS` читаются из
корневого `.env` при каждом запросе; переменные окружения приоритетнее. CLI без ключа
завершается до вызова `Runner.run`. Структурированная генерация в dashboard без ключа
или при ошибке API возвращает явную ошибку; незавершённые строки CSV помечаются как
`not_generated` или `error`, а не как успешные ответы OpenAI.

Node-explainer использует одного агента без инструментов: только предварительно
подготовленные метрики одного узла, role-score, условия допуска, профильные scores
и ограничения выборки. `gid`, список контрагентов и исходные транзакции в prompt
не включаются. Трассировка SDK отключена, Responses `store=False`. Ключ хранится
только на backend; `.env` и скрытые файлы недоступны через HTTP. Проверенные ответы
сохраняются в `runtime/narratives/` с ключом по метрикам, модели и версии prompt.

Раздел **«AI-объяснения для CSV»** обрабатывает все узлы либо top-список после явного
подтверждения платных запросов. Есть прогресс, остановка и продолжение. Обязательные
`evidence`/`why` дополнены полными объяснениями и структурированными числовыми фактами.
[Формат, ограничения и API](../docs/api_evidence_exports.md).

Проверки используют подменённый вызов модели, а не реальный API. Платный ответ можно
проверить после добавления своего ключа: открыть любой узел и нажать кнопку объяснения.

Реализация следует [Agents SDK quickstart](https://developers.openai.com/api/docs/guides/agents/quickstart)
и [настройкам моделей](https://developers.openai.com/api/docs/guides/agents/models).

## Проверка

```powershell
python -m unittest discover -s agent_app/tests -v
python agent_app/evals/run_local.py
```

Eval harness выполняет реальные локальные действия, записывает отчёт в
`agent_app/evals/results/latest.json` и возвращает ненулевой код при нарушении
контракта.
