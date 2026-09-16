/*
 * Board sounds, synthesised with the Web Audio API.
 *
 * No audio files on purpose. Chess move sounds are short percussive hits that
 * synthesise convincingly in a few lines, and generating them avoids shipping
 * assets whose licensing would need checking — lichess's and chess.com's sound
 * packs are not ours to copy. It also means the app stays entirely offline and
 * adds nothing to the image.
 *
 * Browsers refuse to start an AudioContext before a user gesture, so the
 * context is created lazily on the first sound and resumed on the first
 * pointer event. Every call is wrapped: audio must never be able to break a
 * move.
 */

let ctx = null
let enabled = true
let noiseBuffer = null

function context() {
  if (ctx === null) {
    const Ctor = window.AudioContext || window.webkitAudioContext
    if (!Ctor) return null
    try {
      ctx = new Ctor()
    } catch {
      return null
    }
  }
  if (ctx.state === "suspended") ctx.resume().catch(() => {})
  return ctx
}

// Unlock on the first interaction so the first move is not silent.
const unlock = () => context()
window.addEventListener("pointerdown", unlock, {once: true})
window.addEventListener("keydown", unlock, {once: true})

export function setEnabled(on) {
  enabled = !!on
}

export function isEnabled() {
  return enabled
}

/** A short burst of filtered noise — the "tap" of wood on wood. */
function noise(ac, when, {gain = 0.12, duration = 0.03, cutoff = 2600} = {}) {
  if (noiseBuffer === null) {
    noiseBuffer = ac.createBuffer(1, ac.sampleRate * 0.2, ac.sampleRate)
    const data = noiseBuffer.getChannelData(0)
    for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1
  }
  const src = ac.createBufferSource()
  src.buffer = noiseBuffer

  const filter = ac.createBiquadFilter()
  filter.type = "lowpass"
  filter.frequency.value = cutoff

  const env = ac.createGain()
  env.gain.setValueAtTime(gain, when)
  env.gain.exponentialRampToValueAtTime(0.0001, when + duration)

  src.connect(filter).connect(env).connect(ac.destination)
  src.start(when)
  src.stop(when + duration + 0.02)
}

/** One pitched element with an exponential decay. */
function tone(ac, when, {freq, to = null, type = "triangle", gain = 0.18, duration = 0.09}) {
  const osc = ac.createOscillator()
  osc.type = type
  osc.frequency.setValueAtTime(freq, when)
  if (to !== null) osc.frequency.exponentialRampToValueAtTime(to, when + duration)

  const env = ac.createGain()
  // A tiny attack instead of an instant start; a hard edge clicks.
  env.gain.setValueAtTime(0.0001, when)
  env.gain.exponentialRampToValueAtTime(gain, when + 0.004)
  env.gain.exponentialRampToValueAtTime(0.0001, when + duration)

  osc.connect(env).connect(ac.destination)
  osc.start(when)
  osc.stop(when + duration + 0.02)
}

const VOICES = {
  /** A piece set down: a soft low thock. */
  move(ac, t) {
    tone(ac, t, {freq: 190, to: 120, gain: 0.17, duration: 0.085})
    noise(ac, t, {gain: 0.07, duration: 0.022, cutoff: 2200})
  },

  /** Heavier and grittier than a move — you should hear that wood was taken. */
  capture(ac, t) {
    tone(ac, t, {freq: 150, to: 82, type: "sawtooth", gain: 0.15, duration: 0.11})
    noise(ac, t, {gain: 0.16, duration: 0.05, cutoff: 3200})
  },

  /** Two quick thocks: king, then rook. */
  castle(ac, t) {
    VOICES.move(ac, t)
    VOICES.move(ac, t + 0.085)
  },

  /** A bright two-note alert that cuts through the thocks. */
  check(ac, t) {
    tone(ac, t, {freq: 720, type: "square", gain: 0.1, duration: 0.07})
    tone(ac, t + 0.075, {freq: 960, type: "square", gain: 0.1, duration: 0.09})
  },

  promote(ac, t) {
    ;[523, 659, 880].forEach((f, i) =>
      tone(ac, t + i * 0.06, {freq: f, gain: 0.13, duration: 0.11}))
  },

  /** Rising major triad. */
  win(ac, t) {
    ;[523, 659, 784, 1047].forEach((f, i) =>
      tone(ac, t + i * 0.1, {freq: f, type: "triangle", gain: 0.15, duration: 0.3}))
  },

  /** Falling minor line — clearly bad news, but not a klaxon. */
  lose(ac, t) {
    ;[392, 330, 262].forEach((f, i) =>
      tone(ac, t + i * 0.13, {freq: f, type: "triangle", gain: 0.15, duration: 0.34}))
  },

  /** Two equal notes: neither good nor bad. */
  draw(ac, t) {
    tone(ac, t, {freq: 440, gain: 0.13, duration: 0.24})
    tone(ac, t + 0.14, {freq: 440, gain: 0.13, duration: 0.3})
  },
}

export function play(name, delay = 0) {
  if (!enabled) return
  const voice = VOICES[name]
  if (!voice) return
  try {
    const ac = context()
    if (!ac) return
    voice(ac, ac.currentTime + delay)
  } catch {
    /* audio is a nicety; never let it break the game */
  }
}

/**
 * Pick the sound a move should make, from its SAN alone.
 *
 * Order matters: mate is announced by the result sound, so a mating move plays
 * its own capture/move sound and lets the outcome speak for itself. Check beats
 * capture because the check is the more urgent fact.
 */
export function voiceForSan(san) {
  if (!san) return null
  if (san.includes("#")) return san.includes("x") ? "capture" : "move"
  if (san.includes("+")) return "check"
  if (san.startsWith("O-O")) return "castle"
  if (san.includes("=")) return "promote"
  if (san.includes("x")) return "capture"
  return "move"
}
