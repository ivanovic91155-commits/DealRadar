# Подключение OpenAI API к DealRadar

Инструкция под ваш реальный деплой: **Railway**, запуск `python -m deal_radar --config config.json run`
(`railway.json`). Секреты туда попадают только через Variables сервиса.

Проверено 8 августа 2026. Интерфейс OpenAI меняется — если названия разделов
разошлись с текстом, ориентируйтесь на смысл шага.

> **Ключ никогда не присылайте в чат, Telegram, issue или скриншот.** Он не должен
> попасть в код, `config.json`, `.env.example` и логи. Я его не спрошу.

---

## Шаг 1. Разделить ChatGPT и API

Подписка ChatGPT Plus и баланс OpenAI API — **разные продукты**. Наличие Plus не
даёт доступа к API: у API отдельный кошелёк.

Откройте <https://platform.openai.com/> и войдите тем же аккаунтом.

## Шаг 2. Пополнить баланс и поставить лимит

1. Биллинг: <https://platform.openai.com/settings/organization/billing/overview>
   Добавьте способ оплаты или кредиты. Для старта хватит минимальной суммы —
   расчётный расход DealRadar ниже.
2. Лимиты: <https://platform.openai.com/settings/organization/limits>
   Поставьте месячный лимит расходов. Это ваша страховка **вне** приложения;
   внутренний дневной бюджет DealRadar — вторая, независимая.

**Сколько это стоит.** Level 1 работает на `gpt-5.6-luna`: $0.20 за 1M входных и
$1.20 за 1M выходных токенов. Одно объявление — примерно 1500 входных и 400
выходных токенов, то есть **около $0.0008**. При 300 новых объявлениях в сутки
это **≈ $0.25 в день**. Дневной бюджет по умолчанию — $5, с запасом в 20 раз.

## Шаг 3. Отдельный проект

<https://platform.openai.com/settings/organization/projects> → создайте проект,
например `DealRadar Production`. Отдельный проект нужен, чтобы видеть расход
именно этого бота и чтобы ключ можно было отозвать, не задев остальное.

## Шаг 4. Создать ключ

<https://platform.openai.com/api-keys> → **Create new secret key**, привязав его к
проекту `DealRadar Production`.

Ключ показывается **один раз**. Сразу сохраните его в менеджер паролей.

## Шаг 5. Положить ключ в Railway

В Railway: проект → сервис DealRadar → вкладка **Variables** → **New Variable**.

Обязательная переменная:

```
OPENAI_API_KEY=<ключ из шага 4>
```

Включение AI (по умолчанию слой выключен):

```
AI_ANALYSIS_ENABLED=true
AI_SHADOW_MODE=true
AI_CAN_AFFECT_DEAL_STATUS=false
```

`AI_SHADOW_MODE=true` — это и есть этап A: AI анализирует и пишет результат в
базу, но **не влияет** ни на статусы сделок, ни на Telegram. Так и надо для
первого запуска.

Остальные переменные (модели, бюджет, цены токенов) имеют рабочие значения по
умолчанию — задавайте их, только если хотите отклониться. Полный список с
комментариями лежит в [`.env.example`](../.env.example).

**AI-оценка цены (Level 2)** включается отдельно и по умолчанию выключена:

```
AI_PRICE_ESTIMATE_ENABLED=true
```

Она работает на `gpt-5.6-terra` и добавляет порядка $0.5 в день сверх Level 1.
Включайте её после того, как посмотрите на shadow-данные Level 1 и убедитесь,
что модель правильно распознаёт велосипеды. Оценённая цена может довести сделку
до INTERESTING, но не до HOT — это отдельный предохранитель `AI_PRICE_ALLOW_HOT`.

После сохранения переменных Railway перезапустит сервис сам.

## Шаг 6. Проверить

Локально (ключ должен быть в вашем локальном `.env`, который в git не попадает):

```bash
python -m deal_radar --config config.json ai-check --live
```

Ожидаемый вывод:

```
AI enabled: yes
API key configured: yes
Shadow mode: yes
Primary model configured: gpt-5.6-luna
Fallback model configured: gpt-5.6-terra
Daily budget: $5.00 (stop at budget: True)
Prompt version: listing-analysis-v1.0.0
Schema version: dealradar.ai-analysis.v1
Structured Output test: passed (gpt-5.6-luna, 47 tokens, $0.000015)
```

Без `--live` команда ничего не тратит и проверяет только конфигурацию.

Затем разбор одного объявления из фикстуры — один вызов, без Telegram и без
изменения production-данных:

```bash
python -m deal_radar --config config.json ai-test-listing --fixture tests/fixtures/ai/trek_marlin.json
```

В конце выводится строка с токенами и стоимостью вызова.

## Шаг 7. Смотреть за расходом

- Внутри DealRadar: таблица `ai_call_log` в SQLite — по строке на каждый вызов,
  с токенами, стоимостью и результатом. Дневной бюджет считается по ней же,
  поэтому переживает перезапуск процесса.
- В логах цикла: ключи `ai_calls`, `ai_cache_hits`, `ai_skipped`, `ai_pending`,
  `ai_failed`, `ai_cost_usd`.
- У OpenAI: <https://platform.openai.com/settings/organization/usage>

## Если что-то пошло не так

| Симптом | Причина и что делать |
|---|---|
| `API key configured: no (OPENAI_API_KEY)` | Переменная не задана в Railway Variables либо в локальном `.env`. |
| `Structured Output test: FAILED — ... HTTP 401` | Ключ неверен или отозван. Создайте новый в шаге 4. |
| `HTTP 429` в логах | Превышен rate limit. Клиент сам делает ретраи с backoff; при постоянных 429 поднимите лимиты у OpenAI. |
| `AI daily budget reached` | Дневной бюджет исчерпан. Объявления сохраняются как `AI_PENDING` и разберутся после полуночи UTC либо после подъёма `OPENAI_DAILY_BUDGET_USD`. |
| Расход выше ожидаемого | Проверьте `OPENAI_MODEL_PRIMARY`. Алиас `gpt-5.6` маршрутизируется на Sol ($5/$30) — это в 25 раз дороже Luna. Нужен явный ID `gpt-5.6-luna`. |

## Kill switch

Мгновенно выключить AI, не трогая код и не откатывая деплой:

```
AI_ANALYSIS_ENABLED=false
```

Парсер, оценка рынка, deal engine и Telegram продолжат работать как до
подключения AI.

## Если ключ утёк

1. Немедленно отозвать его на <https://platform.openai.com/api-keys> (Revoke).
2. Создать новый и обновить переменную в Railway.
3. Только после этого чистить историю репозитория, если ключ попал в git.

Порядок именно такой: пока старый ключ жив, чистка истории ничего не защищает.
