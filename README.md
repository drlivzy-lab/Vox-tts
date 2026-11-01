# Харнесс озвучки на Gemini 3.8 Flash TTS

Универсальный конвейер озвучки на `gemini-3.8-flash-tts`, собранный из опыта «Сокрушителя Войн»:
многоголосые аудиокниги, а также короткие тексты — слоганы, реплики, реклама, рилсы. Одна
команда `vox.py` (старое имя `book.py` тоже работает); состояние хранится в файлах проекта,
любой шаг можно повторить, ничего не оплачивается дважды.

**Агентам: начинайте с [AGENTS.md](AGENTS.md).** Документы:

| Файл | О чём |
|---|---|
| [AGENTS.md](AGENTS.md) | правила и полный цикл работы агента |
| [docs/GEMINI_TTS.md](docs/GEMINI_TTS.md) | всё о Gemini 3.8 Flash TTS: модели, запросы, голоса, стили, теги, цены, ошибки |
| [docs/SCRIPT_GUIDE.md](docs/SCRIPT_GUIDE.md) | как писать сценарий: книги и реклама, подача, теги, текст «для уха» |
| [docs/MONTAGE.md](docs/MONTAGE.md) | монтаж, паузы, громкость, проверка склеек |
| [docs/QC_ASR.md](docs/QC_ASR.md) | дословное распознавание: GigaAM-v3, Gemini, Whisper |
| [docs/ASSESSMENT.md](docs/ASSESSMENT.md) | оценка качества, доверие агенту, план улучшений |
| [docs/examples/clips_example.md](docs/examples/clips_example.md) | пример сценария клипов |

Агенту удобнее через MCP: `mcp_server.py` (настройка в `.mcp.json`; один раз `python setup_agent_files.py` разложит `.mcp.json`, навык и CI из папки `setup/`) даёт инструменты `vox_next`, `vox_status`,
`vox_show_job` и `vox` для любой бесплатной команды; платное он не выполняет. В Claude Code — навык `.claude/skills/vox`.

## Установка

Python 3.11+ и `numpy`; `ffmpeg` для экспорта и мастера (системный или `pip install imageio-ffmpeg`).
Для дословной проверки — `gigaam` (рекомендуется) или `transformers`, запасные — `faster-whisper`, `vosk`.

```
pip install -r requirements.txt
copy .env.example .env          # впишите GEMINI_API_KEY=...
python -m pytest tests          # офлайн, без сети и оплат
```

На Windows запускайте `python -X utf8 vox.py ...`. Ключ берётся из переменной окружения, затем из
`.env` проекта, затем из `.env` харнесса. Ключ не пишется в журналы и не коммитится (`.env` в `.gitignore`).
Проект по умолчанию можно задать в `.env`: `VOX_PROJECT=projects/studio`.

## Одна фраза: tts.py

```
python tts.py "Привет! Это проверка." --voice Kore --style "Warm, upbeat." -o hello.wav          # покажет запрос
python tts.py "Привет! Это проверка." --voice Kore --style "Warm, upbeat." -o hello.wav --yes    # отправит (платно)
python tts.py --dialogue duo.txt --speakers "Аня=Kore,Олег=Puck" -o duo.wav --yes
python tts.py --list-voices
```

Без кэша и журнала — для быстрых проб. Для работы, которую нужно хранить, используйте проект.

## Короткие тексты: проект clips

```
python vox.py init projects/studio --kind clips
python vox.py --book projects/studio say "Скидка 20 % до пятницы" --voice Puck --style "Energetic, smiling." --takes 3 --yes
python vox.py --book projects/studio clips plan clips/spring.md
python vox.py --book projects/studio clips render clips/spring.md --takes 3 --formats wav,mp3 --preset social --yes
```

Сценарий — блоки как у книги (см. `docs/examples/clips_example.md`), у каждого голос, подача и
текст; `mode: conversational` — диалог двух студийных голосов одним запросом. Результат:
`out/<сценарий>/<блок>-tN.<формат>`, `.srt` и `index.md` со списком дублей. Громкость по пресету
(`social` −14, `podcast` −16, `audiobook` −19, `acx` −20, `broadcast` −23 LUFS).

## Книга

```
python vox.py init projects/kniga --source "путь/к/книге.fb2"
set VOX_PROJECT=projects/kniga        # или --book projects/kniga у каждой команды
python vox.py next                     # что делать дальше, с готовой командой
python vox.py ingest
python vox.py cast add --role VASHER --name Вашер --gender male --alias Вашер --profile V02
python vox.py draft
python vox.py attribute prepare        # пакеты для ИИ-агента -> attribution/responses/*.json
python vox.py attribute import         # строгая проверка; сомнительное -> attribution/REVIEW.md
python vox.py pron scan
python vox.py check
python vox.py pilot plan --sections s003   # пилот на одной главе: темп, голоса, подача, цена
python vox.py pilot run --yes              # платно; отчёт pilot/CALIBRATION.md
python vox.py approve s001 s002
python vox.py freeze
python vox.py plan                     # объём, токены, цена
python vox.py synth --yes              # Batch по умолчанию; --mode flex для проб
python vox.py batch collect --all
python vox.py qc run                   # дословное распознавание -> qc/<релиз>/REPORT.md
python vox.py review                   # qc/<релиз>/review.html: слушать, принять/отклонить
python vox.py qc apply decisions-r002.json
python vox.py assemble
python vox.py qc splices --asr         # проверка склеек -> SPLICES.md
python vox.py master
python vox.py publish --library ../library --cover cover.jpg   # книга в библиотеку плеера (player/)
python vox.py status
python vox.py costs                    # costs.html: потрачено, осталось, по дням
```

Несколько агентов размечают книгу параллельно: `attribute claim --worker a1 --count 3` выдаёт каждому свои
пакеты, `attribute status` показывает прогресс, `attribution/ROLES.md` — сводку ролей для проверки.
Общая библиотека голосов (`library/voices.json`): `voices add/list/use`, `voices list --expiring 30`.

```
исходник ─ ingest ─▶ абзацы ─ draft ─▶ сценарий ─ attribute ─▶ роли/режим/подача
                                          │
                     cast + pron ─────────┤ check / approve
                                          ▼
                                       freeze ─▶ релиз (точные запросы + хеши)
                                          │
                     plan ─ synth (Batch/Flex, кэш, журнал, лимиты) ─ batch collect
                                          │
                       qc run (GigaAM / Gemini, без подсказки) ─ accept / reject
                                          │
                       assemble ─ qc splices ─▶ главы ─ master ─▶ book.m4b ─ publish ─▶ плеер
```

Плеер для слушателей — [player/README.md](player/README.md): библиотека книг, тёмная контрастная тема,
режим «Авто» для мультимедиа машины, громкость до 200 %, таймер сна, закладки, офлайн.

## Проект книги

| Путь | Что там | Кто меняет |
|---|---|---|
| `book.toml` | настройки (kind = book|clips), (модель, режим, лимиты, паузы, QC, мастер) | человек |
| `source/` | исходник, не меняется; SHA-256 в `text/sections.json` | — |
| `text/` | абзацы `p000001…`, разделы `s001…` | `ingest` один раз |
| `script/s001.md` | сценарий: блоки `### id` + `yaml` + `text` | draft, ИИ, человек |
| `cast/cast.json` | роли → профили → голоса, карточки | `cast`, человек |
| `pronunciation/lexicon.json` | словарь: форма → IPA/ударение, контекст, статус | `pron`, человек |
| `releases/rNNN/` | замороженные запросы и хеши; `ACTIVE.json` | `freeze` |
| `audio/cache/` | WAV по хешу запроса | синтез |
| `audio/ledger.jsonl` | журнал всех попыток (reserve → success/failure/unknown) | синтез |
| `audio/batches/` | пакеты Batch: запрос, карта, маркер отправки | синтез |
| `qc/rNNN/` | проверка каждого запроса, решения слушателя, отчёт | `qc` |
| `chapters/rNNN/` | главы WAV + timeline.json | `assemble` |
| `master/rNNN/` | FLAC частей, `book.m4b` с оглавлением | `master` |

Формат сценария совместим со сценариями «Сокрушителя Войн» (`speaker_id`, `mode`, `style`,
`review_status`, `source_ids`, `scene_id`); голоса переносятся командой
`cast import --path "<папка WB>/casting/voice_plan.json"`.

## Как устроены ключевые решения

**Разбиение.** Абзац делится на речь и ремарки по правилам русской пунктуации («— Реплика, —
сказал он. — …»). Говорящий угадывается по ремарке, если имя есть в касте; остальное размечает
ИИ. Инвариант проверяется везде: буквы и цифры сценария совпадают с исходным абзацем, иначе
нужна явная причина в поле `edit`. Запросы (jobs) собираются из подряд идущих блоков одного
голоса и подачи, до `chunking.max_words` слов (≈30–90 с), без разрыва предложений и в пределах
лимита 8 192 входных токенов.

**Голоса и подача.** Рассказчик нейтрален: `style` пустой, тембр задаёт голос. У персонажей
`style` = карточка роли + «Scene delivery: …» из сценария (`tts.style_policy`). Проверяется, что
в одной сцене разные роли не звучат одним голосом и один голос не закреплён за двумя профилями.
Теги — только из таблицы Google (`<short pause>`, `<sigh>`…); неизвестный тег или `[whispers]`
остановит `check`, потому что прозвучит вслух.

**Произношение.** Словарь подставляется только в текст запроса; литературный текст не меняется.
Принятый у нас способ ударения — замена слова на IPA в слешах (`/xɐˈlandrʲɪn/`). Запись
попадает в релиз только со статусом `accepted` (после `pron trial` и прослушивания); омонимы
различаются полем `context`.

**Кэш и релизы.** Хеш запроса = модель + голос + style + точный текст. Новый релиз после правки
одной реплики синтезирует только её, остальное берётся из кэша. Одинаковые запросы в разных
местах книги синтезируются один раз.

**Повторы и лимиты.** Каждая попытка записывается в журнал до отправки. POST никогда не
повторяется автоматически: оборванный запрос мог быть оплачен, поэтому он блокируется до
`ledger resolve`. На запрос не больше `limits.max_attempts_per_request` попыток; каждое
`qc reject` даёт ровно одну новую. 429 останавливает весь запуск (квоты бывают суточными).
Пакет Batch отправляется один раз: маркер создаётся эксклюзивно. Лимиты запуска —
`max_requests_per_run`, `max_chars_per_run`; всё платное — только с `--yes` после плана.

**Режимы.** `batch` (по умолчанию) и `flex` стоят вдвое дешевле Standard; Batch асинхронный,
Flex отвечает сразу, но может ждать свободной мощности. Standard включается только явно.

**Проверка аудио.** Сразу при получении: формат, тишина, клиппинг, скорость речи (слишком быстро —
пропуск, слишком медленно — зацикливание), длинные паузы внутри. `qc run` распознаёт речь без
подсказки ожидаемого текста и ищет лишние слова, пропуски, повторы фраз, обрыв конца и повтор на
стыке соседних запросов. Это кандидаты: решение — `qc accept` или `qc reject` после прослушивания.
`master` не собирается при открытых подозрениях (тестовая сборка — `--allow-open`).

**Монтаж и мастер.** Тишина по краям срезается с запасом и плавными краями; паузы задаются картой
(`pauses`: продолжение, смена говорящего, абзац, сцена, заголовок) или `pause_after_ms` у блока.
Мысли (`mode: thought`) получают лёгкую реверберацию. Каждая глава — двухпроходный loudnorm до
цели пресета (по умолчанию −19 LUFS) с контрольным замером, 2 с между главами, M4B AAC 64 кбит/с с оглавлением.

## Атрибуция ИИ

`attribute prepare` пишет пакеты `attribution/packets/<раздел>-NN.md` с правилами, списком ролей
и блоками. Отвечать может любой ИИ-агент (бесплатно, в рамках его работы) — JSON кладётся в
`attribution/responses/`. Или `attribute run --yes` отправит пакеты текстовой модели Gemini
(`llm.model` в `book.toml`). `attribute import` принимает ответ только целиком корректный:
каждый блок покрыт ровно один раз, роли известны или объявлены в `new_roles`, split не меняет
слов, у рассказчика нет style. Новые роли появляются в касте без голоса — `check` это покажет.

## Проверка кода харнесса

`python -X utf8 check.py` — компиляция и офлайн-тесты харнесса (`cd player; npm test` — тесты плеера); `check.py --install-hook` — перед каждым коммитом;
`.github/workflows/tests.yml` — то же в GitHub (Windows и Linux).

## Чего пока нет

- Проверки на реальном API: первый запуск — пилот с малым бюджетом.

Оценка и список улучшений v3 — [docs/ASSESSMENT.md](docs/ASSESSMENT.md).
