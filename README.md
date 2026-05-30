# Zamira Russian Bot — Deploy qilish

## Fayl strukturasi
```
zamira-bot/
├── main.py          ← Asosiy fayl (bot + server)
├── requirements.txt
├── railway.toml
└── static/
    └── index.html   ← Telegram Web App
```

## Railway ga deploy qilish

### 1. GitHub ga push qiling
```bash
git init
git add .
git commit -m "first commit"
git remote add origin https://github.com/SIZNING_USERNAME/zamira-bot.git
git push -u origin main
```

### 2. Railway da yangi project
- railway.app ga kiring
- "New Project" → "Deploy from GitHub repo"
- Repo ni tanlang

### 3. Environment variables qo'shing
Railway dashboard → Variables bo'limi:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | BotFather dan olgan token |
| `WEBAPP_URL` | https://SIZNING-APP.up.railway.app |

> ⚠️ WEBAPP_URL ni deploy bo'lgandan keyin Railway bergan URL bilan to'ldiring

### 4. Deploy tugagach
- Railway URL ni oling (masalan: `https://zamira-bot-production.up.railway.app`)
- Shu URL ni `WEBAPP_URL` ga qo'ying
- Redeploy qiling

### 5. BotFather da WebApp ruxsati
BotFather ga yozing:
```
/setmenubutton
```
Bot ni tanlang → URL kiriting → nom kiriting

### Test qilish
Botga `/start` yozing — "📚 Darsni boshlash" tugmasi chiqishi kerak.
