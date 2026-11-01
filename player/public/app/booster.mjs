// Громкость 50–200% и «выравнивание для дороги».
// Пока усиление выше 100% и выравнивание ни разу не включали, Web Audio не создаётся:
// звук идёт напрямую из <audio>, как в обычном плеере (так надёжнее на iPhone в фоне).
// После включения цепочка постоянная: источник → усиление → [компрессор] → лимитер → выход.

export const isIOS = () => /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

export class Booster {
  constructor(audio) {
    this.audio = audio;
    this.boost = 1; this.leveling = false; this.fade = 1;
    this.ctx = null;
    // Web Audio создаём только после жеста пользователя: иначе контекст стартует «на паузе»
    // и звук из кнопок на руле или автозапуска пропал бы.
    this.allowBuild = false;
  }
  get active() { return Boolean(this.ctx); }
  get supported() { return Boolean(window.AudioContext || window.webkitAudioContext); }

  build() {
    if (this.ctx || !this.supported) return Boolean(this.ctx);
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx({ latencyHint: 'playback' });
      const source = ctx.createMediaElementSource(this.audio);
      const gain = ctx.createGain();
      const leveler = ctx.createDynamicsCompressor();
      leveler.threshold.value = -30; leveler.knee.value = 12; leveler.ratio.value = 3.5;
      leveler.attack.value = 0.01; leveler.release.value = 0.3;
      const makeup = ctx.createGain(); makeup.gain.value = 1.6; // +4 дБ после компрессии
      const limiter = ctx.createDynamicsCompressor();
      limiter.threshold.value = -1.5; limiter.knee.value = 0; limiter.ratio.value = 20;
      limiter.attack.value = 0.002; limiter.release.value = 0.08;
      source.connect(gain);
      limiter.connect(ctx.destination);
      Object.assign(this, { ctx, source, gain, leveler, makeup, limiter });
      this.route();
      this.audio.volume = 1;
      return true;
    } catch { this.ctx = null; return false; }
  }

  route() {
    const { gain, leveler, makeup, limiter } = this;
    try { gain.disconnect(); leveler.disconnect(); makeup.disconnect(); } catch {}
    if (this.leveling) { gain.connect(leveler); leveler.connect(makeup); makeup.connect(limiter); }
    else gain.connect(limiter);
  }

  apply() {
    const level = this.boost * this.fade;
    if (!this.ctx && this.allowBuild && (this.boost > 1 || this.leveling)) this.build();
    if (this.ctx) {
      this.gain.gain.setTargetAtTime(level, this.ctx.currentTime, 0.05);
      this.audio.volume = 1;
    } else {
      this.audio.volume = Math.min(1, level);
    }
  }

  setBoost(value) { this.boost = value; this.apply(); }
  setLeveling(on) { this.leveling = on; if (this.ctx) this.route(); this.apply(); }
  unlock() { this.allowBuild = true; this.apply(); }
  setFade(value) { this.fade = value; this.apply(); }
  // Контекст после паузы или блокировки может быть приостановлен браузером.
  resume() { if (this.ctx && this.ctx.state !== 'running') return this.ctx.resume().catch(() => {}); return Promise.resolve(); }
}
