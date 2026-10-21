# Пример сценария коротких озвучек

Каждый блок — отдельная озвучка; `takes` — сколько дублей сделать.
Рендер: `python vox.py clips render clips/example.md --yes` (сначала `clips plan`).

### slogan

```yaml
voice: Kore
style: "Warm, confident, a smile in the voice."
takes: 3
```

```text
Вкус, который ПОМНИШЬ. <short pause> Новый «Северный мёд».
```

### reels-hook

```yaml
voice: Puck
style: "Intriguing, conspiratorial, leaning in close."
takes: 2
```

```text
Вы точно выбрасываете это каждый день… а зря.
```

### radio-duo

```yaml
speakers: [Аня=Kore, Олег=Puck]
style: "Friendly radio ad banter."
takes: 2
```

```text
Аня: Ты уже пробовал новый кофе?
Олег: |угу| Третий день не могу остановиться!
Аня: Тогда скажи всем, где его взять.
Олег: Во всех кофейнях «Зерно» — до конца месяца по сто девяносто девять рублей!
```
