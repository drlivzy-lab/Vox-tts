# Gemini 3.8 Flash TTS: полный справочник для агента

Сверено с официальной документацией Google 21.10.2026 и с опытом «Сокрушителя Войн»
(≈9 600 запросов). Метки: **[Google]** — описано разработчиком; **[проверено]** — проверено нами
на слух; **[сообщество]** — опубликованный чужой опыт; **[осторожно]** — не подтверждено.

Источники: [руководство TTS](https://ai.google.dev/gemini-api/docs/speech-generation),
[карточка модели](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-tts),
[Voice design](https://ai.google.dev/gemini-api/docs/voice-design),
[Voice replication](https://ai.google.dev/gemini-api/docs/voice-replication),
[Batch API](https://ai.google.dev/gemini-api/docs/batch-api),
[цены](https://ai.google.dev/gemini-api/docs/pricing),
[понимание аудио](https://ai.google.dev/gemini-api/docs/generate-content/audio).

## 1. Модели

| Модель | Для чего | Языки |
|---|---|---|
| `gemini-3.8-flash-tts` | максимум качества: актёрская игра, диалоги, длинное чтение | 130+ |
| `gemini-3.8-flash-lite-tts` | дёшево и быстро: массовые короткие фразы, голосовые агенты | 100+ |

**[Google]** Обе модели используют одну схему запроса. Вход — только текст, выход — только аудио.
Лимиты: 8 192 входных токена, 16 384 выходных. Аудио считается ≈25 токенов в секунду,
так что 16 384 токена — это теоретически ≈10 минут. На практике держим запрос в пределах 30–90 секунд:
на длинных запросах чаще дрейфует тембр и появляются повторы.
Поддерживаются Batch API, Flex и Priority. Не поддерживаются structured output, кэш контекста,
function calling, thinking и Live API.
Язык не задаётся: модель определяет его по тексту. Поля `language` в схеме нет.

## 2. Запрос: Interactions API (основной)

`POST https://generativelanguage.googleapis.com/v1beta/interactions`, заголовок `x-goog-api-key`.

```json
{
  "model": "gemini-3.8-flash-tts",
  "service_tier": "flex",
  "input": [{"type": "user_input", "content": [{
    "type": "text",
    "text": "Ты хочешь уйти <short pause> именно СЕЙЧАС?",
    "annotations": [{"type": "speech_metadata", "style": "Quietly insistent, restrained anxiety."}]
  }]}],
  "response_format": {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000},
  "generation_config": {"speech_config": [{"voice": "Kore"}]},
  "stream": false
}
```

| Поле | Значение |
|---|---|
| `model` | модель из §1 |
| `service_tier` | `standard`, `flex` (вдвое дешевле, может ждать мощности), `priority` (дороже, быстрее) **[Google; priority через этот маршрут нами не проверен]** |
| `input[].content[]` | один или несколько блоков `type: text`; `text` — дословно произносимый текст |
| `annotations[].type` | `speech_metadata` |
| `speech_metadata.style` | подача этого блока: эмоция, темп, громкость, отношение (свободный английский текст) |
| `speech_metadata.speaker` | имя говорящего; только в диалоге, в каждой реплике |
| `response_format.mime_type` | `audio/wav` (по умолчанию для обычного ответа), `audio/l16`, `audio/mulaw`, `audio/alaw` |
| `response_format.sample_rate` | Гц: 24000 (по умолчанию), 16000, 8000 |
| `generation_config.speech_config` | один голос: массив `[{"voice": "..."}]`; диалог: объект (§5) |
| `stream` | `true`: события `step.delta` с `delta.type == "audio"`, куски `audio/l16` без заголовка |

Ответ REST: `steps[]` → элемент `type: "model_output"` → `content[]` → `type: "audio"`, `data` (base64).
В Python SDK (`google-genai` ≥ 2.25.0): `client.interactions.create(...)`, затем `interaction.output_audio.data`.

**Форматы.** `audio/wav`: RIFF, PCM 16 бит, моно, 24 кГц, заголовок 44 байта. `audio/l16`: тот же PCM без
заголовка (по умолчанию в стриме). `audio/mulaw` и `audio/alaw`: 8 бит G.711 для телефонии.
MP3/AAC модель не отдаёт, их делает ffmpeg (`vox/post.py`). WAV нельзя склеивать сложением байтов:
у каждого файла свой заголовок.

## 3. Запрос: generateContent и Batch

Batch работает через `POST /v1beta/models/gemini-3.8-flash-tts:batchGenerateContent`. В пакете
inline-запросы (до ≈20 МБ на весь пакет, крупнее — через Files API):

```json
{"batch": {"display_name": "book r001 s003", "input_config": {"requests": {"requests": [
  {"request": {
     "contents": [{"role": "user", "parts": [{"text": "…", "speech_metadata": {"style": "…"}}]}],
     "generationConfig": {"responseModalities": ["AUDIO"],
                          "speechConfig": {"voiceConfig": {"voice": "voice_2bavt005meqj"}}}},
   "metadata": {"key": "s003-j0001"}}]}}}}
```

**[проверено]** Эта форма отработала на всех 61 разделе «Сокрушителя Войн». Поле `service_tier` в Batch
не передаётся. Статус проверяется `GET /v1beta/batches/<id>`, поле `metadata.state`:
`BATCH_STATE_PENDING`, `…RUNNING`, `…SUCCEEDED`, `…FAILED`, `…CANCELLED`, `…EXPIRED`.
Результаты лежат в `response.inlinedResponses.inlinedResponses[]`, где у каждого есть `metadata.key` и
`response.candidates[0].content.parts[].inlineData` (`mimeType` + `data`). Обычно пакет готов за минуты
или часы, срок выполнения до 24 часов.
**Большие пакеты: Files API.** Inline-пакет ограничен ≈20 МБ запроса. Крупнее — входной JSONL-файл
(до 2 ГБ): строки `{"key": "s003-j0001", "request": {…тот же GenerateContentRequest…}}`. Файл загружается
resumable-загрузкой: `POST /upload/v1beta/files` с заголовками `X-Goog-Upload-Protocol: resumable`,
`X-Goog-Upload-Command: start`, `X-Goog-Upload-Header-Content-Length`, `…-Content-Type: application/jsonl`;
ответный заголовок `X-Goog-Upload-URL` → `POST` туда с `X-Goog-Upload-Command: upload, finalize` и
`X-Goog-Upload-Offset: 0`; в ответе `file.name` = `files/…`. Пакет: `"input_config": {"file_name": "files/…"}`.
Результат тогда приходит файлом: `response.responsesFile` → `GET /download/v1beta/files/…:download?alt=media`,
строки `{"key", "response"}` или `{"key", "error"}`. Файлы хранятся у Google 48 часов, загрузка бесплатна.
Харнесс: `limits.batch_input = "auto"` (по умолчанию inline, если помещается, иначе файл), `"file"` или
`"inline"`; ответ сохраняется в `audio/batches/<пакет>/responses.jsonl`. **[не проверено на TTS]** Форма
взята из документации Batch API для generateContent; первый файловый пакет запускайте маленьким
(`limits.batch_input = "file"` и 3–5 запросов).

**[Google]** Для generateContent упоминается вариант `AUDIO_L16`. Наш сборщик понимает и WAV, и
`audio/L16;rate=24000`.

## 4. Голоса

### Студийные (30) [Google]
| Голос | Характер | Голос | Характер | Голос | Характер |
|---|---|---|---|---|---|
| Zephyr | яркий | Puck | бодрый | Charon | информативный |
| Kore | твёрдый | Fenrir | возбуждённый | Leda | юный |
| Orus | твёрдый | Aoede | лёгкий | Callirrhoe | непринуждённый |
| Autonoe | яркий | Enceladus | с придыханием | Iapetus | ясный |
| Umbriel | непринуждённый | Algieba | ровный | Despina | ровный |
| Erinome | ясный | Algenib | с хрипотцой | Rasalgethi | информативный |
| Laomedeia | бодрый | Achernar | мягкий | Alnilam | твёрдый |
| Schedar | ровный | Gacrux | зрелый | Pulcherrima | напористый |
| Achird | дружелюбный | Zubenelgenubi | небрежный | Vindemiatrix | нежный |
| Sadachbia | живой | Sadaltager | знающий | Sulafat | тёплый |

Все голоса читают по-русски: язык определяется по тексту. Код языка в каталоге — это метаданные голоса,
а не список языков, которые он умеет читать. Выбор делаем только после русской пробы
(`vox cast audition`). **[проверено]** В Warbreaker пригодились Alnilam и Umbriel.

### Расширенная библиотека
`GET /v1beta/voices` (`vox cast voices`, `tts.py --list-voices`). Фильтры: `language_code`, `region_code`,
`accent`, `gender` (female/male/neutral), `pitch` (low/medium/high), `persona`, `context`,
`type` (prebuilt/prompted/replicated), `search`, `page_size` (≤1000), `page_token`. Несколько значений
внутри одного фильтра объединяются через ИЛИ, разные фильтры через И. Созданные вами голоса идут первыми.

### Voice design: голос по описанию
`POST /v1beta/voices`:
```json
{"store": true, "voice": {"model": "gemini-3.8-flash-tts", "type": "prompted", "display_name": "Narrator",
  "gender": "male", "language_code": "ru-RU",
  "prompted": {"input": "A mature male Russian audiobook narrator with a warm, velvety low baritone, rounded chest resonance and clear natural diction. Measured baseline pace, restrained warmth."}}}
```
Ответ содержит `id` (`voice_…`) и `sample_audio` (WAV-пробу). Описание пишется по-английски, 1–2 предложения:
возраст, пол, тембр, фактура, акцент, базовая манера. Ситуативные эмоции сюда не входят, для них есть `style`.
Лимит — 200 сохранённых голосов на проект (общий для prompted и replicated). Голос живёт год после
последнего использования, каждое использование продлевает срок. Харнесс: `vox cast design --profile V05 --yes`.

### Voice replication: клон голоса
`type: "replicated"`, поля `replicated.source_audio` (10–30 с чистой речи, WAV mono PCM16 24 кГц) и
`replicated.consent_audio` (запись согласия того же совершеннолетнего человека). С `store: true` получается
`voice_…`, с `store: false` — `voicekey_…`, который живёт 7 дней. Клон воспроизводит тембр, но не исправляет
произношение имён. **Клонировать голос можно только с согласия его владельца.**

## 5. Диалог двух голосов одним запросом

```json
"input": [{"type": "user_input", "content": [
  {"type": "text", "text": "Ты уже пробовал новый кофе?", "annotations": [{"type": "speech_metadata", "speaker": "Аня"}]},
  {"type": "text", "text": "|угу| Третий день не могу остановиться!", "annotations": [{"type": "speech_metadata", "speaker": "Олег", "style": "Excited."}]}
]}],
"generation_config": {"speech_config": {"mode": "conversational",
  "speakers": [{"speaker": "Аня", "voice": "Kore"}, {"speaker": "Олег", "voice": "Puck"}]}}
```
**[Google]** В одном запросе максимум 2 говорящих, только студийные голоса, `speaker` нужен в каждой реплике.
`mode: conversational` доступен только в REST и даёт естественные перехваты реплик. Вставка `|угу|` внутри
реплики — это реакция слушателя, она может наложиться на речь. Голоса `voice_…` в диалоге не работают:
каждую реплику синтезируют отдельно и склеивают.
Харнесс: блок клипа со `speakers: [Аня=Kore, Олег=Puck]` или `tts.py --dialogue`. В Batch диалог не отправляем.

## 6. Управление подачей

### style (`speech_metadata.style`)
Свободный английский текст про ЭТУ реплику: эмоция, темп, громкость, высота, отношение.
**[Google]** Рекомендации разработчика:
- Сначала пробуйте пустой style и добавляйте короткие указания только там, где они нужны.
- Одинаковую подачу повторяйте дословно, так звучание стабильнее.
- Постоянные черты (возраст, пол, имя, акцент) в style не пишут: их задаёт голос (Voice design).
- Мета-указания вроде «сохраняй тот же тембр» не пишут: они **усиливают** дрейф.
- Смена подачи посреди фразы оформляется отдельным блоком с новым style.

Проверенные формулировки и их эффект — в [SCRIPT_GUIDE.md](SCRIPT_GUIDE.md#словарь-подачи).
**[сообщество]** Указание «быстро» ускоряло речь Flash примерно в 1,4 раза. Противоречивые указания
(«кричи» + `<whispers>`) дают неестественный результат. Модель сильно опирается на смысл фразы:
«сыграть злость» на ласковом тексте почти не получается.

### Встроенные теги (в `text`, английские, в ASCII-скобках `< >`, без закрывающих)
| Группа | Теги |
|---|---|
| Дыхание | `<breath>` `<heavy breath>` `<exhales>` `<pant>` `<gasp>` `<yawn>` `<pff>` `<phew>` `<sigh>` `<sighs>` `<argh>` |
| Смех | `<laugh>` `<laughter>` `<chuckle>` `<chuckles>` `<giggle>` `<snicker>` `<cackle>` `<cheer>` |
| Плач и боль | `<cry>` `<sob>` `<whimper>` `<moan>` `<groan>` |
| Крик и рык | `<scream>` `<shout>` `<shriek>` `<growl>` `<grunt>` `<grr>` `<hiss>` `<tsk>` |
| Физиология | `<cough>` `<sneeze>` `<snort>` `<throat-clearing>` |
| Шёпот | `<whispers>` `<whispering>` |
| Паузы | `<short pause>` `<long pause>` |

Теги — разовые события, а не режим всей реплики. Шёпот на всю реплику задаётся через style.
Звуковые эффекты (аплодисменты, удар, дверь) модель не делает, их добавляют при монтаже.
Квадратные теги `[whispers]` и блоки «Audio Profile» из старых моделей 3.8 **прочитает вслух**.
`vox check` пропускает только теги из таблицы.
**[сообщество]** `<long pause>` после предложения длился 1,5–3,1 с, `<short pause>` почти не отличался от
обычной точки. Если нужна точная пауза, её ставят при монтаже (`pause_after_ms`).

### Акцент, ритм, естественность
- Слово в ВЕРХНЕМ РЕГИСТРЕ получает логическое ударение. Одно на фразу, иначе получится крик.
- Пунктуация управляет ритмом: «…», «—», запятые. Восклицательный знак повышает энергию.
- Для живой речи можно прямо писать оговорки и паузы-заполнители («ну… э-э, я хотел сказать»),
  но в книге только там, где они есть у автора.

## 7. Произношение

**[проверено]** Надёжный способ задать ударение — **заменить слово на IPA в слешах**:
`Я произношу слово /zɐˈmok/.` → «замо́к», `/ˈzamək/` → «за́мок». Знак `ˈ` ставится перед ударным слогом,
гласные пишутся с учётом редукции. Транскрипция после слова («замок /zɐˈmok/») не проверялась.
**[проверено, частично]** Знак ударения U+0301 («Ва́р») помог на односложном имени. Для длинных слов
надёжнее IPA.
**[Google]** Отдельного словаря произношения или SSML у модели нет. `<phoneme>`, `<prosody>`,
`<break time>` и `<say-as>` относятся к Cloud TTS / Chirp 3 и сюда не переносятся. Поля
`customPronunciations` из Cloud TTS тоже не работают.
Харнесс: `pronunciation/lexicon.json` (форма → IPA, `context` для омонимов, `status: accepted` только после
пробы). Подстановка идёт только в текст запроса. Числа, даты и сокращения пишите словами в нужном падеже,
латиницу — кириллицей или проверенной транскрипцией.

## 8. Тарифы и лимиты

| Тариф | Ввод, $/1M | Аудио, $/1M | ≈ за час аудио |
|---|---|---|---|
| Standard | 0,50 | 9,00 | ≈ $0,81 |
| Batch / Flex | 0,25 | 4,50 | ≈ $0,41 |
| Priority | 0,90 | 16,20 | ≈ $1,46 |

Цены на 2026 год по [разбору eesel](https://eesel.ai/blog/gemini-3-8-flash-tts-pricing). Объявлено, что
с 01.01.2027 они удвоятся. Перед расчётом сверяйтесь с [официальной страницей](https://ai.google.dev/gemini-api/docs/pricing).
Харнесс считает стоимость по `[pricing]` в `book.toml`.

**Лимиты запросов (RPM/RPD) зависят от уровня проекта** (Tier) и смотрятся в AI Studio.
**[проверено]** 429 `RESOURCE_EXHAUSTED` бывает суточным. Отправлять такие запросы повторно бесполезно,
поэтому харнесс при 429 останавливает весь запуск. Случался и `content_blocked`: отдельный фрагмент пролога
отклонялся фильтром, а после разбиения на части прошёл.

## 9. Ошибки и что с ними делать

| Ситуация | Признак | Действие |
|---|---|---|
| Квота | HTTP 429 | остановиться; ждать `Retry-After` или следующих суток; не повторять в цикле |
| Перегрузка | 500/503 (часто на Flex) | GET-запросы повторять с паузой; POST — только через журнал и лимит попыток |
| Тайм-аут POST | нет ответа | исход неизвестен, запрос мог быть оплачен: `ledger resolve` после проверки, не повторять сразу |
| Фильтр | `content_blocked`, `finishReason != STOP` | переформулировать или разбить текст; литературный текст не цензурировать без решения человека |
| Пустое/короткое аудио | техника: «подозрительно коротко» | пропуск текста; `qc reject` и перегенерация |
| Зацикливание | «подозрительно долго», повтор в ASR | `qc reject`; укоротить запрос |
| Дрейф тембра | на слух, аномалия «смена тембра» | короче запросы, пустой style у рассказчика, отдельные запросы на подачу |

## 10. Чего не делать

- Отправлять Markdown, YAML, имена говорящих или режиссёрские пометки в `text`: всё это прочитается вслух.
- Ставить `temperature`, `top_p` или `seed` в TTS-запрос как управление: в схеме Interactions для TTS их нет.
  **[сообщество]** В интерфейсах temperature ниже 1 давала тишину и обрывы.
- Ждать побайтового повтора: тот же запрос даёт новое исполнение. Принятые дубли храните, а не перегенерируйте.
- Слепо переносить «фишки» сторонних обёрток (`speed`, `voice2`, MP3 на выходе): это их обработка, не Google.
