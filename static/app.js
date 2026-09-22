import {Chessboard, COLOR, INPUT_EVENT_TYPE, FEN} from "./vendor/cm-chessboard/src/Chessboard.js"
import {Markers, MARKER_TYPE} from "./vendor/cm-chessboard/src/extensions/markers/Markers.js"
import {Arrows, ARROW_TYPE} from "./vendor/cm-chessboard/src/extensions/arrows/Arrows.js"
import {
  PromotionDialog,
  PROMOTION_DIALOG_RESULT_TYPE,
} from "./vendor/cm-chessboard/src/extensions/promotion-dialog/PromotionDialog.js"

import * as sound from "./sound.js"

const ASSETS = "/static/vendor/cm-chessboard/assets/"
const $ = (id) => document.getElementById(id)
const api = async (path, options) => {
  const res = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    ...options,
  })
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
  return res.json()
}

const MODE_HINTS = {
  drill: "The bot follows your chosen line, then plays naturally at your level. Leaving the line is flagged, not punished.",
  strict: "The bot stays in book theory as long as it can. Best for memorising a repertoire.",
  realistic: "No book at all. Maia already plays the openings real players at your rating play — deviations included.",
}

const state = {game: null, chosenOpening: null, review: null, reviewGameId: null,
               cursor: 0, viewPly: null, rep: null}

/* ------------------------------------------------------------- boards -- */

function makeBoard(el, opts = {}) {
  return new Chessboard(el, {
    position: FEN.start,
    assetsUrl: ASSETS,
    style: {cssClass: "default", pieces: {file: "pieces/staunty.svg"}, borderType: "frame"},
    extensions: [{class: Markers}, {class: Arrows}, {class: PromotionDialog}],
    ...opts,
  })
}

const playBoard = makeBoard($("board"))
let reviewBoard = null

/**
 * cm-chessboard throws "moveInput already enabled" if input is enabled twice,
 * and an exception here aborts whatever was mid-render — which is how resuming
 * a game from the list stopped switching to the Play tab. Always disable first.
 */
function setMoveInput(board, handler, colour) {
  board.disableMoveInput()
  if (handler) board.enableMoveInput(handler, colour)
}

/* -------------------------------------------------------------- tabs --- */

document.querySelectorAll("[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => showView(btn.dataset.view))
})

function showView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("is-active"))
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"))
  $("view-" + name).classList.add("is-active")
  document.querySelector(`.tab[data-view="${name}"]`)?.classList.add("is-active")
  // Opening Review always lands on the list of games; a review itself is a
  // detail view *inside* that tab rather than a separate destination you can
  // get stranded in.
  if (name === "review" && !state.reviewGameId) showReviewList()
  if (name === "puzzles") loadPuzzles().catch((e) => console.error(e))
  if (name === "reps" && !$("rep-family").options.length) loadFamilies().catch(console.error)
}

/* ------------------------------------------------------------- meters -- */

async function saveSetting(patch) {
  try {
    const r = await api("/api/settings", {method: "POST", body: JSON.stringify(patch)})
    state.settings = r.settings
  } catch (err) {
    console.error("could not save setting", err)
  }
}

$("opt-mate-hints").addEventListener("change", (e) =>
  saveSetting({mate_hints: e.target.checked}))
const OFFSET_HINTS = {
  "-300": "A clearly weaker opponent. Use this to practise converting, not to grind rating.",
  "-150": "A step down from where the ladder has you. Good while you are learning a new opening.",
  "0": "Wherever the ladder has settled — aimed at you scoring a bit under half.",
  "150": "A step up. Expect to lose more than you win.",
}
function updateOffsetHint() {
  $("offset-hint").textContent = OFFSET_HINTS[$("opt-offset").value] || ""
}
$("opt-offset").addEventListener("change", (e) => {
  updateOffsetHint()
  saveSetting({opponent_offset: Number(e.target.value)})
})

$("opt-material").addEventListener("change", (e) =>
  saveSetting({material_hints: e.target.checked}))

$("opt-sounds").addEventListener("change", (e) => {
  sound.setEnabled(e.target.checked)
  // Play the click you just enabled, so the toggle confirms itself.
  if (e.target.checked) sound.play("move")
  saveSetting({sounds: e.target.checked})
})
$("opt-mate-max").addEventListener("change", (e) =>
  saveSetting({mate_hint_max: Number(e.target.value)}))

async function loadGuardTags() {
  const d = await api("/api/settings")
  state.settings = d.settings
  $("guard-tags").innerHTML = d.tags
    .map((t) => {
      const badge = t.games
        ? `<span class="tag-count">${t.games} of ${d.reviewed_games} games</span>`
        : `<span class="tag-count is-clean">not seen yet</span>`
      return `<label class="guard-tag">
          <input type="checkbox" data-motif="${t.motif}" ${t.guarded ? "checked" : ""}>
          <span class="tag-body">
            <span class="tag-name">${escapeHtml(t.title)}${badge}</span>
            <p class="tag-advice">${escapeHtml(t.advice)}</p>
          </span>
        </label>`
    })
    .join("")
  $("guard-tags").querySelectorAll("input").forEach((box) => {
    box.addEventListener("change", () => {
      const on = [...$("guard-tags").querySelectorAll("input:checked")]
        .map((b) => b.dataset.motif)
      saveSetting({guards: on})
    })
  })
}

async function refreshState() {
  const s = await api("/api/state")
  state.settings = s.settings || {}
  $("opt-mate-hints").checked = state.settings.mate_hints !== false
  $("opt-mate-max").value = String(state.settings.mate_hint_max ?? 3)
  $("opt-material").checked = state.settings.material_hints !== false
  $("opt-sounds").checked = state.settings.sounds !== false
  sound.setEnabled(state.settings.sounds !== false)
  $("opt-offset").value = String(state.settings.opponent_offset ?? 0)
  updateOffsetHint()
  loadGuardTags().catch((e) => console.error(e))
  $("m-rating").textContent = Math.round(s.rating)
  $("m-rd").textContent = s.confident ? "settled" : `±${Math.round(s.rd)} provisional`
  // Show what the next game is actually played at, not the pre-offset value.
  const effective = s.effective_bot_elo ?? s.bot_elo
  $("m-bot").textContent = effective
  const shifted = effective !== s.bot_elo
  const sub = document.querySelector("#m-bot + .meter-sub")
  sub.textContent = s.offset_clamped
    ? `at the floor — raise "opponent strength"`
    : shifted
      ? `${s.bot_elo} auto, shifted ${effective - s.bot_elo}`
      : "auto-tuned"
  sub.classList.toggle("warn-text", !!s.offset_clamped)
  $("m-form").innerHTML = (s.recent || [])
    .map((r) => `<i class="${r === 1 ? "w" : r === 0 ? "l" : "d"}"></i>`)
    .join("")
  if (!s.maia_available) {
    $("g-status").textContent = "Maia is unavailable — the opponent cannot play."
  }
}

/* ---------------------------------------------------------- openings --- */

let searchTimer = null
$("opening-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer)
  const q = e.target.value.trim()
  if (q.length < 2) return hideResults()
  searchTimer = setTimeout(() => runSearch(q), 160)
})

async function runSearch(q) {
  const hits = await api(`/api/openings?q=${encodeURIComponent(q)}&limit=12`)
  const box = $("opening-results")
  if (!hits.length) return hideResults()
  box.innerHTML = ""
  hits.forEach((o) => {
    const b = document.createElement("button")
    b.type = "button"
    b.innerHTML =
      `<span class="eco">${o.eco}</span>${escapeHtml(o.name)}` +
      `<span class="line">${o.line.join(" ")}</span>`
    b.addEventListener("click", () => {
      state.chosenOpening = o
      $("opening-search").value = o.name
      $("opening-chosen").textContent = `${o.eco} · ${o.line.join(" ")}`
      $("opening-chosen").hidden = false
      hideResults()
    })
    box.appendChild(b)
  })
  box.hidden = false
}

const hideResults = () => ($("opening-results").hidden = true)
document.addEventListener("click", (e) => {
  if (!e.target.closest(".field")) hideResults()
})

$("mode").addEventListener("change", updateModeHint)
function updateModeHint() {
  $("mode-hint").textContent = MODE_HINTS[$("mode").value]
}
updateModeHint()

/* ------------------------------------------------------------ playing -- */

$("btn-new").addEventListener("click", async () => {
  $("btn-new").disabled = true
  try {
    const game = await api("/api/games", {
      method: "POST",
      body: JSON.stringify({
        color: $("color").value,
        mode: $("mode").value,
        opening: state.chosenOpening?.name || $("opening-search").value.trim() || null,
        drill_plies: 12,
      }),
    })
    applyGame(game)
    $("setup").hidden = true
    $("game-info").hidden = false
  } catch (err) {
    $("g-status").textContent = err.message
  } finally {
    $("btn-new").disabled = false
  }
})

$("btn-resign").addEventListener("click", async () => {
  if (!state.game || state.game.is_over) return
  applyGame(await api(`/api/games/${state.game.id}/resign`, {method: "POST"}))
})

$("btn-review-this").addEventListener("click", () => {
  if (!state.game) return
  const opening = state.game.opening ? state.game.opening.name : "unnamed opening"
  startReview(state.game.id, `just now · ${opening} · you were ${state.game.player_color}`)
})

async function answerGuard(action) {
  if (!state.game) return
  for (const id of ["guard-undo", "guard-keep"]) $(id).disabled = true
  try {
    applyGame(await api(`/api/games/${state.game.id}/${action}`, {method: "POST"}))
    await refreshState()
  } catch (err) {
    $("g-status").textContent = err.message
  } finally {
    for (const id of ["guard-undo", "guard-keep"]) $(id).disabled = false
  }
}
$("guard-undo").addEventListener("click", () => answerGuard("undo"))
$("guard-keep").addEventListener("click", () => answerGuard("keep"))

$("btn-newgame").addEventListener("click", () => {
  // Abandoning a game in progress is a real decision; finishing one is not.
  if (state.game && !state.game.is_over &&
      !confirm("That game is still in progress. Start a new one anyway?")) {
    return
  }
  resetToSetup()
})

function resetToSetup() {
  state.game = null
  playBoard.disableMoveInput()
  playBoard.setPosition(FEN.start, true)
  playBoard.removeMarkers()
  playBoard.setOrientation(COLOR.white)
  $("setup").hidden = false
  $("game-info").hidden = true
  $("btn-resign").hidden = true
  $("btn-review-this").hidden = true
  $("btn-newgame").hidden = true
  $("g-line-warning").hidden = true
  $("mate-hint").hidden = true
  $("material-hint").hidden = true
  $("guard").hidden = true
  $("board-overlay").hidden = true
  $("g-status").textContent = ""
  $("movelist").innerHTML = ""
  showView("play")
  refreshState().catch(() => {})
}

/** The square a king stands on, read straight out of the FEN placement field. */
function kingSquare(fen, white) {
  const rows = fen.split(" ")[0].split("/")
  const target = white ? "K" : "k"
  for (let r = 0; r < 8; r++) {
    let file = 0
    for (const ch of rows[r]) {
      if (ch >= "1" && ch <= "8") { file += Number(ch); continue }
      if (ch === target) return "abcdefgh"[file] + (8 - r)
      file++
    }
  }
  return null
}

const DRAW_REASONS = {
  // Headline already says "Stalemate", so the detail explains rather than repeats.
  stalemate: "No legal move left, but the king is not in check — so it is a draw.",
  insufficient_material: "Neither side has enough material to force mate.",
  threefold_repetition: "The same position occurred three times.",
  fivefold_repetition: "The same position occurred five times.",
  fifty_moves: "Fifty moves passed with no capture and no pawn move.",
  seventyfive_moves: "Seventy-five moves passed with no capture and no pawn move.",
}

function outcomeOf(game) {
  if (game.result === "1/2-1/2") {
    return {
      kind: "draw",
      headline: game.termination === "stalemate" ? "Stalemate" : "Draw",
      detail: DRAW_REASONS[game.termination] || "The game ended in a draw.",
      voice: "draw",
    }
  }
  const won =
    (game.result === "1-0" && game.player_color === "white") ||
    (game.result === "0-1" && game.player_color === "black")
  const byMate = game.termination === "checkmate"
  return {
    kind: won ? "win" : "loss",
    headline: won ? "You won" : "You lost",
    detail: byMate
      ? (won ? "Checkmate." : "Checkmate — look for what you allowed.")
      : (won ? "Your opponent resigned." : "You resigned."),
    voice: won ? "win" : "lose",
  }
}

/** Sound the moves that appeared since the last render, then the result. */
function announce(game, previousCount) {
  if (previousCount === null) return   // first paint or a resume: stay quiet
  const fresh = game.san.slice(previousCount)
  fresh.forEach((san, i) => {
    const voice = sound.voiceForSan(san)
    if (voice) sound.play(voice, i * 0.17)
  })
  if (game.is_over) sound.play(outcomeOf(game).voice, fresh.length * 0.17 + 0.3)
}

function showOutcome(game) {
  const overlay = $("board-overlay")
  if (!game.is_over) {
    overlay.hidden = true
    return
  }
  const outcome = outcomeOf(game)
  $("result-card").className = `result-card is-${outcome.kind}`
  $("result-headline").textContent = outcome.headline
  $("result-detail").textContent = outcome.detail
  overlay.hidden = false

  // Mark the king that got mated or stalemated — the answer to "why did it
  // end?" is almost always that square.
  const loser = game.turn === "white"
  const square = kingSquare(game.fen, loser)
  if (square && game.termination === "checkmate") {
    playBoard.addMarker(MARKER_TYPE.circleDangerFilled, square)
  } else if (square && game.termination === "stalemate") {
    playBoard.addMarker(MARKER_TYPE.circleDanger, square)
  }
}

// Click the banner away to study the final position.
$("board-overlay").addEventListener("click", () => {
  $("board-overlay").hidden = true
})

function applyGame(game) {
  const previousCount =
    state.game && state.game.id === game.id ? state.game.san.length : null
  state.game = game
  playBoard.setPosition(game.fen, true)
  playBoard.setOrientation(game.player_color === "black" ? COLOR.black : COLOR.white)

  $("g-opening").textContent = game.opening
    ? `${game.opening.eco} ${game.opening.name}`
    : "—"
  $("g-bot").textContent = `Maia-3 at ${game.bot_elo}`
  $("g-mode").textContent = game.mode

  const hint = $("mate-hint")
  if (game.mate_hint && !game.is_over) {
    const n = game.mate_hint.in
    hint.textContent = n === 1
      ? "There is mate in 1 here. Can you find it?"
      : `There is a forced mate in ${n} here. Can you find it?`
    hint.hidden = false
  } else {
    hint.hidden = true
  }

  const guard = $("guard")
  if (game.pending) {
    guard.hidden = false
    $("guard-title").textContent = "Hold on — take another look"
    $("guard-message").textContent = game.pending.message
    sound.play("check")
  } else {
    guard.hidden = true
  }
  $("g-tb-label").hidden = !game.takebacks
  $("g-takebacks").hidden = !game.takebacks
  $("g-takebacks").textContent = game.takebacks
    ? `${game.takebacks} — this game will not move your rating`
    : "0"

  const material = $("material-hint")
  if (game.material_hint && !game.is_over) {
    material.textContent =
      "Your opponent has left something hanging. Can you see what?"
    material.hidden = false
  } else {
    material.hidden = true
  }

  if (game.left_line) {
    $("g-line-warning").hidden = false
    $("g-line-warning").textContent =
      `You left the drill line here (it played ${game.left_line.expected}). ` +
      `That's fine — the bot will now just play on naturally.`
  }

  renderMoveList(game.san)
  state.viewPly = null
  $("p-live").hidden = true
  $("p-viewing").textContent = ""

  $("btn-newgame").hidden = false
  $("btn-resign").hidden = game.is_over
  $("btn-review-this").hidden = !game.is_over
  if (game.is_over) {
    playBoard.disableMoveInput()
    $("g-status").textContent = describeResult(game)
  } else {
    $("g-status").textContent = game.pending
      ? "Waiting on you."
      : game.turn === game.player_color
        ? "Your move."
        : "Bot is thinking…"
    const colour = game.player_color === "black" ? COLOR.black : COLOR.white
    // A pending warning freezes the board — answer it before moving on.
    setMoveInput(playBoard, game.pending ? null : moveInput, colour)
  }

  highlightLast(playBoard, game.moves)
  showOutcome(game)
  announce(game, previousCount)
}

/* ------------------------------------------------------ play history -- */
/*
 * Stepping back through your own game while it is still running. The board
 * becomes a viewer: move input is off, and any attempt to play jumps back to
 * the live position first so you cannot accidentally move from a past one.
 */

function renderPlayAt(ply) {
  const game = state.game
  if (!game) return
  const live = ply === null || ply >= game.moves.length
  state.viewPly = live ? null : ply

  const shown = live ? game.moves : game.moves.slice(0, ply + 1)
  playBoard.setPosition(live ? game.fen : replay(shown), true)
  highlightLast(playBoard, shown)

  $("p-live").hidden = live
  $("p-viewing").textContent = live
    ? ""
    : `viewing move ${Math.floor(ply / 2) + 1}${ply % 2 ? "…" : "."} of ${Math.ceil(game.moves.length / 2)}`

  setMoveInput(
    playBoard,
    live && !game.is_over && !game.pending ? moveInput : null,
    game.player_color === "black" ? COLOR.black : COLOR.white,
  )
  document.querySelectorAll("#movelist button").forEach((b) => {
    b.classList.toggle("is-current", !live && Number(b.dataset.ply) === ply)
  })
}

function stepPlay(delta) {
  const game = state.game
  if (!game || !game.moves.length) return
  const from = state.viewPly === null ? game.moves.length - 1 : state.viewPly
  renderPlayAt(Math.max(0, Math.min(from + delta, game.moves.length - 1)))
}

$("p-prev").addEventListener("click", () => stepPlay(-1))
$("p-next").addEventListener("click", () => stepPlay(1))
$("p-first").addEventListener("click", () => renderPlayAt(0))
$("p-live").addEventListener("click", () => renderPlayAt(null))
document.addEventListener("keydown", (e) => {
  if (!$("view-play").classList.contains("is-active") || !state.game) return
  if (e.target.matches("input, select, textarea")) return
  if (e.key === "ArrowLeft") { e.preventDefault(); stepPlay(-1) }
  if (e.key === "ArrowRight") { e.preventDefault(); stepPlay(1) }
})

function describeResult(game) {
  const won =
    (game.result === "1-0" && game.player_color === "white") ||
    (game.result === "0-1" && game.player_color === "black")
  if (game.result === "1/2-1/2") return `Draw by ${game.termination}.`
  return won ? `You won by ${game.termination}.` : `You lost by ${game.termination}.`
}

function moveInput(event) {
  // Show where the piece you are holding can actually go. cm-chessboard draws
  // a dot on empty squares and a bevel on capturable ones.
  if (event.type === INPUT_EVENT_TYPE.moveInputStarted) {
    const targets = legalTargets(event.squareFrom)
    if (!targets.length) return false        // nothing to do with this piece
    playBoard.addLegalMovesMarkers(targets)
    return true
  }
  if (event.type === INPUT_EVENT_TYPE.moveInputCanceled ||
      event.type === INPUT_EVENT_TYPE.moveInputFinished) {
    playBoard.removeLegalMovesMarkers()
    return true
  }

  if (event.type === INPUT_EVENT_TYPE.validateMoveInput) {
    const plain = event.squareFrom + event.squareTo
    const legal = state.game.legal_moves
    if (legal.includes(plain)) {
      submitMove(plain)
      return true
    }
    // Promotion: the same from/to exists four times with a suffix.
    const promos = legal.filter((m) => m.startsWith(plain) && m.length === 5)
    if (promos.length) {
      event.chessboard.showPromotionDialog(event.squareTo, event.piece[0] === "w" ? COLOR.white : COLOR.black,
        (result) => {
          if (result && result.type === PROMOTION_DIALOG_RESULT_TYPE.pieceSelected) {
            submitMove(plain + result.piece[1])
          } else {
            playBoard.setPosition(state.game.fen, true)
          }
        })
      return true
    }
    return false
  }
  return true
}

/** Legal destinations from a square, shaped the way the Markers extension wants. */
function legalTargets(from) {
  if (!state.game || state.game.is_over) return []
  const seen = new Set()
  const out = []
  for (const uci of state.game.legal_moves) {
    if (!uci.startsWith(from)) continue
    const to = uci.slice(2, 4)
    if (seen.has(to)) continue          // four promotion moves share one square
    seen.add(to)
    out.push({from, to, promotion: uci.length === 5 ? uci[4] : undefined})
  }
  return out
}

async function submitMove(uci) {
  if (state.viewPly !== null) renderPlayAt(null)
  $("g-status").textContent = "Bot is thinking…"
  playBoard.disableMoveInput()
  try {
    applyGame(await api(`/api/games/${state.game.id}/moves`, {
      method: "POST",
      body: JSON.stringify({uci}),
    }))
    await refreshState()
  } catch (err) {
    $("g-status").textContent = err.message
    playBoard.removeLegalMovesMarkers()
    playBoard.setPosition(state.game.fen, true)
    if (!state.game.is_over) {
      setMoveInput(playBoard, moveInput,
        state.game.player_color === "black" ? COLOR.black : COLOR.white)
    }
  }
}

function highlightLast(board, moves) {
  board.removeMarkers()
  if (!moves || !moves.length) return
  const last = moves[moves.length - 1]
  board.addMarker(MARKER_TYPE.framePrimary, last.slice(0, 2))
  board.addMarker(MARKER_TYPE.framePrimary, last.slice(2, 4))
}

function renderMoveList(san) {
  const list = $("movelist")
  list.innerHTML = ""
  const cell = (ply) => {
    const li = document.createElement("li")
    if (san[ply] === undefined) return li
    const b = document.createElement("button")
    b.dataset.ply = ply
    b.textContent = san[ply]
    b.addEventListener("click", () => renderPlayAt(ply))
    li.appendChild(b)
    return li
  }
  for (let i = 0; i < san.length; i += 2) {
    list.insertAdjacentHTML("beforeend", `<li class="num">${i / 2 + 1}.</li>`)
    list.appendChild(cell(i))
    list.appendChild(cell(i + 1))
  }
  list.scrollTop = list.scrollHeight
  $("play-nav").hidden = san.length === 0
}

/* ------------------------------------------------------------- review -- */

/* ----------------------------------------------------------- progress -- */
/*
 * The cross-game view. A per-game review tells you what happened once; this is
 * the only thing in the app that can say "this is a pattern", which is what a
 * learning gradient actually needs.
 */

function bars(rows, valueKey, labelKey, format) {
  const max = Math.max(...rows.map((r) => r[valueKey]), 1)
  return rows
    .map(
      (r) => `<div class="bar-row">
        <span class="bar-label">${escapeHtml(r[labelKey])}</span>
        <span class="bar-track"><i style="width:${Math.max(3, (r[valueKey] / max) * 100)}%"></i></span>
        <span class="bar-value">${escapeHtml(format(r))}</span>
      </div>`,
    )
    .join("")
}

function trendText(t, unit, lowerIsBetter) {
  if (!t) return ""          // fewer than six games is noise, not a trend
  const arrow = t.improving ? "↑" : "↓"
  const word = t.improving ? "better" : "worse"
  const delta = Math.abs(t.change)
  return `<span class="${t.improving ? "up" : "down"}">${arrow} ${delta}${unit} ${word}</span>`
}

async function loadProgress() {
  let d
  try {
    const scope = state.statsScope || "all"
    d = await api(`/api/stats${scope === "all" ? "" : `?source=${scope}`}`)
  } catch (err) {
    return
  }
  // The filter lives outside the panel it filters. Hiding the card when a
  // scope has nothing in it would take the filter with it and strand you on an
  // empty screen with no way back — the same dead end the Review tab had.
  const anywhere = d.sources
    ? d.sources.trainer.reviewed + d.sources.chesscom.reviewed
    : d.games
  $("stats-card").hidden = !anywhere
  labelStatsScopes(d.sources)

  $("progress-card").hidden = !d.games
  $("stats-empty").hidden = Boolean(d.games)
  if (!d.games) {
    $("stats-empty").textContent = state.statsScope === "chesscom"
      ? "No imported games reviewed yet — import some above."
      : state.statsScope === "trainer"
        ? "No trainer games reviewed yet. Play one and review it."
        : "Nothing reviewed yet."
    return
  }
  $("prog-headline").textContent = d.headline
  $("prog-games").textContent = d.games
  $("prog-record").textContent =
    `${d.record.wins}W ${d.record.losses}L${d.record.draws ? ` ${d.record.draws}D` : ""}`
  $("prog-acc").textContent = d.accuracy.mean != null ? `${d.accuracy.mean}%` : "–"
  $("prog-acc-trend").innerHTML = trendText(d.accuracy.trend, "%")
  // Shown as a rate so a longer game cannot look like a decline.
  $("prog-blun").textContent = `${d.blunders.per_100 ?? d.blunders.per_game}`
  $("prog-blun-trend").innerHTML = trendText(d.blunders.trend, "")
  $("prog-aware").textContent =
    d.awareness.pct != null ? `${d.awareness.pct}%` : "–"
  $("prog-aware-sub").textContent =
    d.awareness.chances ? `${d.awareness.taken} of ${d.awareness.chances}` : ""

  $("prog-weaknesses").innerHTML = d.weaknesses
    .map(
      (w) => `<div class="weakness">
        <h3>${escapeHtml(w.title)}
          <span class="weak-share">in ${w.games} of ${d.games} games</span></h3>
        <p>${escapeHtml(w.advice)}</p>
      </div>`,
    )
    .join("")

  $("prog-phase").innerHTML = bars(d.by_phase, "per_100", "phase",
    (r) => `${r.per_100} per 100 moves`)
  $("prog-piece").innerHTML = bars(d.by_piece, "blunders", "piece",
    (r) => `${r.blunders}`)
}

function showReviewList() {
  state.reviewGameId = null
  loadProgress().catch((e) => console.error(e))
  loadChessCom().catch((e) => console.error(e))
  $("review-list").hidden = false
  $("review-header").hidden = true
  $("review-progress").hidden = true
  $("review-body").hidden = true
  loadHistory().catch((e) => console.error(e))
}

function showReviewDetail(caption) {
  $("review-list").hidden = true
  $("progress-card").hidden = true
  $("review-header").hidden = false
  $("review-caption").textContent = caption || ""
}

$("review-back").addEventListener("click", () => {
  showView("review")
  showReviewList()
})

async function startReview(gameId, caption) {
  showView("review")
  showReviewDetail(caption)
  $("review-body").hidden = true
  $("review-progress").hidden = false
  $("review-bar").style.width = "0%"
  state.reviewGameId = gameId

  await api(`/api/games/${gameId}/review`, {method: "POST"})
  poll(gameId)
}

async function poll(gameId) {
  const r = await api(`/api/games/${gameId}/review`)
  if (r.status === "done") {
    $("review-progress").hidden = true
    renderReview(gameId, r)
    return
  }
  if (r.status === "error") {
    $("review-progress").hidden = true
    $("review-progress").hidden = false
    $("review-progress").innerHTML =
      `<h2>Analysis failed</h2><p class="hint">${escapeHtml(r.error || "unknown error")}</p>` +
      `<button class="btn" id="review-retry">Try again</button>`
    $("review-retry").addEventListener("click", () => startReview(gameId))
    return
  }
  $("review-bar").style.width = `${Math.round((r.progress || 0) * 100)}%`
  $("review-progress-text").textContent =
    `Stockfish is working through the game — ${Math.round((r.progress || 0) * 100)}%`
  setTimeout(() => poll(gameId), 900)
}

async function renderReview(gameId, payload) {
  const game = await api(`/api/games/${gameId}`)
  state.review = payload.data
  state.reviewGameId = gameId
  state.gameForReview = game

  $("review-body").hidden = false
  $("review-header").hidden = false
  if (!reviewBoard) {
    reviewBoard = makeBoard($("rboard"))
    $("r-prev").addEventListener("click", () => gotoPly(state.cursor - 1))
    $("r-next").addEventListener("click", () => gotoPly(state.cursor + 1))
  }
  reviewBoard.setOrientation(game.player_color === "black" ? COLOR.black : COLOR.white)

  const hero = game.player_color === "white"
  const d = state.review
  $("acc-you").textContent = fmtPct(hero ? d.accuracy.white : d.accuracy.black)
  $("acc-opp").textContent = fmtPct(hero ? d.accuracy.black : d.accuracy.white)
  const acpl = hero ? d.acpl.white : d.acpl.black
  $("acc-acpl").textContent = acpl == null ? "–" : Math.round(acpl)

  renderLessons(game)
  renderReviewMoves(game)
  renderGraph()
  gotoPly(d.lessons.length ? d.lessons[0] : 0)
}

function renderLessons(game) {
  const box = $("lessons")
  box.innerHTML = ""
  const byPly = Object.fromEntries(state.review.moves.map((m) => [m.ply, m]))
  const lessons = state.review.lessons.map((p) => byPly[p]).filter(Boolean)

  if (!lessons.length) {
    box.innerHTML = `<p class="hint">No clear mistakes in this one. Nice game.</p>`
    return
  }

  lessons.forEach((m) => {
    const el = document.createElement("div")
    el.className = `lesson j-${m.judgment}`
    const moveNo = Math.floor(m.ply / 2) + 1
    const dots = m.color === "white" ? "." : "..."

    const common = m.human.systematic
      ? `<span class="badge common">common at your level</span>`
      : ""
    // A thrown-away mate is not well described as "cost 12% winning chances".
    const hadMate = heroMate({...m, mate_white: m.mate_before_white})
    const hasMate = heroMate(m)
    let cost = `cost ${Math.round(m.win_lost)}% winning chances`
    if (hadMate != null && hadMate > 0 && (hasMate == null || hasMate < 0)) {
      cost = `threw away mate in ${hadMate}`
    } else if (hasMate != null && hasMate < 0) {
      cost = `now mated in ${Math.abs(hasMate)}`
    }
    let meta = `${m.judgment} · ${cost} · ${m.phase}`
    if (m.human.played_pct) {
      meta += ` · ${m.human.played_pct}% of players at your rating play it`
    }

    let advice = ""
    if (m.best_move && m.human.best_findable) {
      advice = `<p class="why">Better: <strong>${escapeHtml(m.best_move)}</strong>` +
        (m.best_line.length ? ` — ${escapeHtml(m.best_line.slice(0, 4).join(" "))}` : "") + `</p>`
    } else if (m.best_move) {
      // Telling a beginner to find a move 2% of their peers find is not teaching.
      advice = `<p class="why hint">The engine prefers ${escapeHtml(m.best_move)}, but only
        ${m.human.best_pct}% of players at your rating find it — so the point here is the
        pattern, not that exact move.</p>`
    }

    el.innerHTML =
      `<h3>${moveNo}${dots} ${escapeHtml(m.san)}${common}</h3>` +
      `<div class="meta">${escapeHtml(meta)}</div>` +
      (m.findings.length ? `<p class="why">${escapeHtml(m.findings[0].statement)}</p>` : "") +
      advice +
      `<div class="coach" data-ply="${m.ply}"><em>Loading explanation…</em></div>`

    el.addEventListener("click", () => gotoPly(m.ply))
    box.appendChild(el)
    loadExplanation(m.ply, el.querySelector(".coach"))
  })
}

async function loadExplanation(ply, target) {
  try {
    const r = await api(`/api/games/${state.reviewGameId}/explain/${ply}`)
    target.innerHTML =
      escapeHtml(r.text) +
      `<span class="src">${r.used_llm ? "written by the coach" : "no model available — generated from the analysis"}</span>`
  } catch (err) {
    target.innerHTML = `<span class="src">Explanation unavailable: ${escapeHtml(err.message)}</span>`
  }
}

function renderReviewMoves(game) {
  const list = $("review-moves")
  list.innerHTML = ""
  const moves = state.review.moves
  for (let i = 0; i < moves.length; i += 2) {
    list.insertAdjacentHTML("beforeend", `<li class="num">${i / 2 + 1}.</li>`)
    list.appendChild(moveCell(moves[i]))
    list.appendChild(moveCell(moves[i + 1]))
  }
}

function moveCell(m) {
  const li = document.createElement("li")
  if (!m) return li
  const b = document.createElement("button")
  b.dataset.ply = m.ply
  b.innerHTML =
    escapeHtml(m.san) +
    (m.judgment ? `<span class="tag j-${m.judgment}">${symbolFor(m.judgment)}</span>` : "")
  b.addEventListener("click", () => gotoPly(m.ply))
  li.appendChild(b)
  return li
}

const symbolFor = (j) => ({inaccuracy: "?!", mistake: "?", blunder: "??"}[j] || "")

function gotoPly(ply) {
  const moves = state.gameForReview.moves
  ply = Math.max(0, Math.min(ply, moves.length - 1))
  state.cursor = ply
  const entry = state.review.moves[ply]

  // Show the position *after* the move so the consequence is visible.
  const fenBoard = replay(moves.slice(0, ply + 1))
  reviewBoard.setPosition(fenBoard, true)
  highlightLast(reviewBoard, moves.slice(0, ply + 1))

  reviewBoard.removeArrows()
  // The key only makes sense when there are arrows to explain.
  $("arrow-key").hidden = !(entry && entry.judgment)
  if (entry && entry.judgment) {
    // Red: what you actually played. Always drawn for an error — the earlier
    // version only drew it when a *better* move happened to be findable, which
    // hid the mistake in exactly the positions that were hardest.
    reviewBoard.addArrow(ARROW_TYPE.danger, entry.uci.slice(0, 2), entry.uci.slice(2, 4))

    // Green: what to play instead. Suppressed when almost nobody at this
    // rating finds it — drawing an arrow to an unfindable move teaches the
    // move rather than the idea.
    const showBetter = entry.best_move_uci && entry.human.best_findable
    if (showBetter) {
      reviewBoard.addArrow(ARROW_TYPE.success,
        entry.best_move_uci.slice(0, 2), entry.best_move_uci.slice(2, 4))
    }
    $("arrow-key").classList.toggle("no-better", !showBetter)
    // Amber: how the opponent punishes it, one ply deeper.
    if (entry.refutation_uci && entry.refutation_uci.length) {
      const r = entry.refutation_uci[0]
      reviewBoard.addArrow(ARROW_TYPE.warning, r.slice(0, 2), r.slice(2, 4))
    }
  }

  document.querySelectorAll("#review-moves button").forEach((b) => {
    b.classList.toggle("is-current", Number(b.dataset.ply) === ply)
  })

  if (entry) {
    const pct = heroWin(entry)
    $("evalfill").style.width = `${Math.max(2, Math.min(98, pct))}%`
  }
  updateGraphCursor(ply)
}

/* Replay UCI moves to a FEN without shipping a full chess library to the client.
   cm-chessboard only needs piece placement, so a simple mailbox replay is enough. */
function replay(ucis) {
  const ranks = [
    "rnbqkbnr", "pppppppp", "........", "........",
    "........", "........", "PPPPPPPP", "RNBQKBNR",
  ].map((r) => r.split(""))
  const idx = (sq) => [8 - Number(sq[1]), sq.charCodeAt(0) - 97]

  for (const uci of ucis) {
    const [fr, ff] = idx(uci.slice(0, 2))
    const [tr, tf] = idx(uci.slice(2, 4))
    const piece = ranks[fr][ff]
    ranks[fr][ff] = "."

    // En passant: a pawn moving diagonally onto an empty square captures behind it.
    if (piece.toLowerCase() === "p" && ff !== tf && ranks[tr][tf] === ".") {
      ranks[fr][tf] = "."
    }
    ranks[tr][tf] = uci.length === 5
      ? (piece === piece.toUpperCase() ? uci[4].toUpperCase() : uci[4])
      : piece

    // Castling: the king moves two files, so the rook has to follow.
    if (piece.toLowerCase() === "k" && Math.abs(tf - ff) === 2) {
      const [rookFrom, rookTo] = tf > ff ? [7, 5] : [0, 3]
      ranks[tr][rookTo] = ranks[tr][rookFrom]
      ranks[tr][rookFrom] = "."
    }
  }

  return ranks
    .map((r) => r.join("").replace(/\.+/g, (m) => m.length))
    .join("/")
}

/* -------------------------------------------------------- eval graph -- */
/*
 * One series: your winning chances after every move, from YOUR side of the
 * board, so "down" always means "I lost ground" regardless of colour.
 *
 * Severity is encoded by marker SIZE and OPACITY on a single hue, not by three
 * different hues. The app's amber/orange/red severity colours are fine as text
 * (they always sit next to "?!" / "?" / "??" and the word itself) but as bare
 * dots they would be colour-alone, and the two lighter ones measure ΔE 0.6
 * apart under deuteranopia — indistinguishable. Size survives every kind of
 * colour blindness.
 */

const GRAPH = {w: 600, h: 150, padT: 10, padB: 20, padX: 4}
const SEVERITY = {
  inaccuracy: {r: 3.0, opacity: 0.5, label: "Inaccuracy"},
  mistake:    {r: 4.5, opacity: 0.75, label: "Mistake"},
  blunder:    {r: 6.5, opacity: 1.0, label: "Blunder"},
}

/** Win% from the hero's point of view. Stored value is White-relative. */
function heroWin(entry) {
  const white = state.gameForReview.player_color === "white"
  // A forced mate is not 88% winning, which is what the win% curve returns once
  // mate is flattened to its centipawn ceiling. Pin it to the top or the bottom
  // so the graph shows a decided game as decided.
  const mate = heroMate(entry)
  if (mate != null) return mate > 0 ? 100 : 0
  const v = entry.win_white != null ? entry.win_white : 50
  return white ? v : 100 - v
}

/** Signed mate distance from the hero's side: +3 they mate, -3 they get mated. */
function heroMate(entry) {
  if (entry.mate_white == null) return null
  const white = state.gameForReview.player_color === "white"
  return white ? entry.mate_white : -entry.mate_white
}

/** What the engine actually said, in the engine's own terms where it matters. */
function heroEvalText(entry) {
  const mate = heroMate(entry)
  if (mate == null) return `${Math.round(heroWin(entry))}% winning`
  const n = Math.abs(mate)
  return mate > 0 ? `M${n} for you` : `M${n} against you`
}

const gx = (i, n) => GRAPH.padX + (i / Math.max(1, n - 1)) * (GRAPH.w - 2 * GRAPH.padX)
const gy = (pct) =>
  GRAPH.padT + (1 - pct / 100) * (GRAPH.h - GRAPH.padT - GRAPH.padB)

function renderGraph() {
  const moves = state.review.moves
  const n = moves.length
  const wrap = $("evalgraph")
  if (!n) { wrap.innerHTML = ""; return }

  const pts = moves.map((m, i) => [gx(i, n), gy(heroWin(m))])
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ")
  const base = gy(50)
  // Close the path back along the 50% line, so the shaded lobes read as
  // "distance from equal" on whichever side of equality the game was.
  const area = `${line} L${pts[n - 1][0].toFixed(1)},${base} L${pts[0][0].toFixed(1)},${base} Z`

  const ticks = []
  const every = n > 60 ? 10 : n > 24 ? 5 : 2
  for (let i = 0; i < n; i += every * 2) {
    ticks.push(`<text class="g-tick" x="${gx(i, n).toFixed(1)}" y="${GRAPH.h - 6}">${Math.floor(i / 2) + 1}</text>`)
  }

  const marks = moves
    .map((m, i) => {
      if (!m.judgment) return ""
      const heroMove = (m.color === "white") === (state.gameForReview.player_color === "white")
      if (!heroMove) return ""  // only annotate your own errors
      const spec = SEVERITY[m.judgment]
      return `<circle class="g-mark" data-ply="${m.ply}" cx="${gx(i, n).toFixed(1)}"
                cy="${gy(heroWin(m)).toFixed(1)}" r="${spec.r}"
                opacity="${spec.opacity}"/>`
    })
    .join("")

  wrap.innerHTML = `
    <svg viewBox="0 0 ${GRAPH.w} ${GRAPH.h}" preserveAspectRatio="none"
         class="g-svg" role="img"
         aria-label="Your winning chances after each move, with your mistakes marked">
      <line class="g-base" x1="${GRAPH.padX}" y1="${base}" x2="${GRAPH.w - GRAPH.padX}" y2="${base}"/>
      <text class="g-axis" x="${GRAPH.w - GRAPH.padX}" y="${GRAPH.padT + 8}" text-anchor="end">winning</text>
      <text class="g-axis" x="${GRAPH.w - GRAPH.padX}" y="${base - 4}" text-anchor="end">equal</text>
      <text class="g-axis" x="${GRAPH.w - GRAPH.padX}" y="${GRAPH.h - GRAPH.padB - 3}" text-anchor="end">losing</text>
      <path class="g-area" d="${area}"/>
      <path class="g-line" d="${line}"/>
      ${marks}
      <line class="g-cursor" id="g-cursor" x1="0" y1="${GRAPH.padT}" x2="0" y2="${GRAPH.h - GRAPH.padB}"/>
      <circle class="g-dot" id="g-dot" r="4" cx="-99" cy="-99"/>
      ${ticks.join("")}
      <rect id="g-hit" x="0" y="0" width="${GRAPH.w}" height="${GRAPH.h}" fill="transparent"/>
    </svg>`

  const hero = state.gameForReview.player_color
  $("graph-title").textContent = `How the game swung — your chances as ${hero}`

  const svg = wrap.querySelector("svg")
  const plyAt = (evt) => {
    const box = svg.getBoundingClientRect()
    const frac = (evt.clientX - box.left) / box.width
    const x = frac * GRAPH.w
    const i = Math.round(((x - GRAPH.padX) / (GRAPH.w - 2 * GRAPH.padX)) * (n - 1))
    return Math.max(0, Math.min(n - 1, i))
  }
  svg.addEventListener("mousemove", (e) => showTip(plyAt(e), e))
  svg.addEventListener("mouseleave", () => { $("graph-tip").hidden = true })
  svg.addEventListener("click", (e) => gotoPly(plyAt(e)))
  // Touch: tapping the graph should navigate, not just hover.
  svg.addEventListener("touchstart", (e) => {
    if (e.touches[0]) gotoPly(plyAt(e.touches[0]))
  }, {passive: true})
}

function showTip(ply, evt) {
  const m = state.review.moves[ply]
  if (!m) return
  const tip = $("graph-tip")
  const num = Math.floor(ply / 2) + 1
  const dots = m.color === "white" ? "." : "..."
  const judged = m.judgment ? ` · ${SEVERITY[m.judgment].label}` : ""
  tip.innerHTML =
    `<strong>${num}${dots} ${escapeHtml(m.san)}</strong>${escapeHtml(judged)}` +
    `<span>${escapeHtml(heroEvalText(m))}</span>`
  tip.hidden = false
  const wrapBox = $("graph-wrap").getBoundingClientRect()
  const x = evt.clientX - wrapBox.left
  tip.style.left = `${Math.max(4, Math.min(wrapBox.width - 150, x - 70))}px`
}

function updateGraphCursor(ply) {
  const cursor = document.getElementById("g-cursor")
  const dot = document.getElementById("g-dot")
  if (!cursor || !dot) return
  const n = state.review.moves.length
  const m = state.review.moves[ply]
  if (!m) return
  const x = gx(ply, n)
  cursor.setAttribute("x1", x); cursor.setAttribute("x2", x)
  dot.setAttribute("cx", x); dot.setAttribute("cy", gy(heroWin(m)))
}

/* ------------------------------------------------------------- reps -- */
/*
 * Drilling opening theory. The bot plays the line, you have to find your side
 * of it, and getting it wrong ends the rep and shows you the move you owed.
 */

let repBoard = null

function ensureRepBoard() {
  if (!repBoard) repBoard = makeBoard($("repboard"))
  return repBoard
}

async function loadFamilies() {
  const families = await api("/api/reps/families")
  state.families = Object.fromEntries(families.map((f) => [f.family, f]))
  const select = $("rep-family")
  const previous = select.value
  select.innerHTML = families
    .map((f) => `<option value="${escapeHtml(f.family)}">${escapeHtml(f.family)} — ${escapeHtml(f.side)} (${f.lines} lines)</option>`)
    .join("")
  if (previous && state.families[previous]) select.value = previous
  applyFamilySide()
  await loadRepProgress()
}

/* Each opening belongs to a side — the Sicilian is Black's, the Italian is
   White's — so pick that side by default rather than making you work it out.
   Still changeable: drilling the other side of your own opening is useful. */
function applyFamilySide() {
  const chosen = state.families?.[$("rep-family").value]
  if (!chosen) return
  $("rep-color").value = chosen.side
  describeFamilySide()
}

function describeFamilySide() {
  const chosen = state.families?.[$("rep-family").value]
  if (!chosen) return
  const owner = chosen.side === "white" ? "White" : "Black"
  const playing = $("rep-color").value === chosen.side
  $("rep-side-note").textContent = playing
    ? `The ${chosen.family} is ${owner}'s opening, so you play ${owner}.`
    : `The ${chosen.family} is ${owner}'s opening — you are drilling it from ` +
      `the other side, which is how you learn to meet it.`
}

async function loadRepProgress() {
  const family = $("rep-family").value
  if (!family) return
  const color = $("rep-color").value
  const data = await api(`/api/reps/progress?family=${encodeURIComponent(family)}&color=${color}`)
  state.repProgress = data

  $("rep-progress").hidden = false
  const pct = data.total ? (data.mastered / data.total) * 100 : 0
  $("rep-bar").style.width = `${Math.max(1, pct)}%`
  $("rep-progress-text").textContent =
    `${data.mastered} of ${data.total} lines known · ${data.unlocked} unlocked so far`
  renderRepLines()
}

/* The whole family, not just what the curriculum has unlocked. You cannot
   revise what you cannot find, and picking a line yourself is the only way to
   go back over one you have already banked. */
function renderRepLines() {
  const data = state.repProgress
  if (!data) return
  const scope = state.repScope || "all"
  const shown = data.lines.filter((l) =>
    scope === "known" ? l.mastered : scope === "todo" ? !l.mastered : true)

  $("rep-filter-note").textContent = shown.length
    ? `${shown.length} line${shown.length === 1 ? "" : "s"} — click one to drill it.`
    : scope === "known"
      ? "No lines banked yet. A line banks when you play it twice with no corrections."
      : "Every line in this repertoire is known. Try another opening."

  $("rep-lines").innerHTML = shown
    .map((l) => {
      const mark = l.mastered
        ? '<span class="rep-tick">known</span>'
        : `<span class="rep-streak">${l.streak}/${l.needed}</span>`
      const classes = [
        l.mastered ? "is-known" : "",
        l.key === data.next ? "is-next" : "",
      ].filter(Boolean).join(" ")
      return `<li class="${classes}" data-key="${escapeHtml(l.key)}" tabindex="0" role="button">
          <span class="rep-eco">${escapeHtml(l.eco)}</span>
          <span class="rep-nm">${escapeHtml(l.name)}</span>
          ${mark}
          <span class="rep-mv">${escapeHtml(l.line.join(" "))}</span>
        </li>`
    })
    .join("")
}

$("rep-color").addEventListener("change", () => {
  describeFamilySide()
  loadRepProgress()
})
$("rep-family").addEventListener("change", () => {
  applyFamilySide()
  loadRepProgress()
})
$("rep-start").addEventListener("click", () => startRep())
$("rep-next").addEventListener("click", () => startRep())

// Pick a line to drill. Any line, banked ones included — revising one you
// already know is the point of being able to choose.
$("rep-lines").addEventListener("click", (e) => {
  const row = e.target.closest("li[data-key]")
  if (row) startRep(row.dataset.key)
})
$("rep-lines").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return
  const row = e.target.closest("li[data-key]")
  if (!row) return
  e.preventDefault()
  startRep(row.dataset.key)
})

document.querySelectorAll(".rep-filters .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    state.repScope = chip.dataset.scope
    document.querySelectorAll(".rep-filters .chip")
      .forEach((c) => c.classList.toggle("is-on", c === chip))
    renderRepLines()
  })
})
$("rep-quit").addEventListener("click", () => {
  state.rep = null
  ensureRepBoard().disableMoveInput()
  ensureRepBoard().setPosition(FEN.start, true)
  ensureRepBoard().removeMarkers()
  $("rep-setup").hidden = false
  $("rep-live").hidden = true
  $("rep-status").textContent = ""
  loadRepProgress()
})

$("rep-reset").addEventListener("click", async () => {
  const family = $("rep-family").value
  if (!confirm(`Forget your progress in the ${family}?`)) return
  await api("/api/reps/reset", {
    method: "POST",
    body: JSON.stringify({family, color: $("rep-color").value}),
  })
  loadRepProgress()
})

async function startRep(key) {
  $("rep-start").disabled = true
  try {
    const rep = await api("/api/reps/start", {
      method: "POST",
      body: JSON.stringify({
        family: $("rep-family").value,
        color: $("rep-color").value,
        key: typeof key === "string" ? key : null,
      }),
    })
    $("rep-setup").hidden = true
    $("rep-live").hidden = false
    $("rep-next").hidden = true
    $("rep-result").hidden = true
    $("rep-correction").hidden = true
    applyRep(rep)
  } catch (err) {
    $("rep-status").textContent = err.message
  } finally {
    $("rep-start").disabled = false
  }
}

function applyRep(rep) {
  state.rep = rep
  const board = ensureRepBoard()
  board.setPosition(rep.fen, true)
  board.setOrientation(rep.color === "black" ? COLOR.black : COLOR.white)
  board.removeArrows()
  highlightLast(board, rep.moves)

  $("rep-title").textContent = `Line ${rep.difficulty + 1} of the curriculum`
  $("rep-name").textContent = `${rep.eco} ${rep.line_name}`
  $("rep-len").textContent = `${rep.plies} plies — you play ${Math.ceil(rep.plies / 2)} of them`
  $("rep-pop").textContent =
    rep.popularity >= 40 ? "main line" : rep.popularity >= 10 ? "common" : "sideline"

  if (rep.status === "playing") {
    showCorrection(rep)
    const done = Math.floor(rep.played_plies / 2) + (rep.your_turn ? 1 : 0)
    $("rep-status").textContent = rep.correction
      ? `Play ${rep.correction.expected}.`
      : rep.your_turn
        ? `Your move — ${done} of ${Math.ceil(rep.plies / 2)}.`
        : "…"
    setMoveInput(board, repMoveInput, rep.color === "black" ? COLOR.black : COLOR.white)
  } else {
    $("rep-correction").hidden = true
    board.disableMoveInput()
    showRepResult(rep)
  }
}

/* A wrong move is not the end of the rep any more: the move is taken back, the
   reason is shown, and the board is handed straight back to you. The move you
   owed is drawn on the board as well as named — at 600 reading "d6" and finding
   the pawn are not the same skill, and this exercise is about the line. */
function showCorrection(rep) {
  const panel = $("rep-correction")
  const c = rep.correction
  if (!c) { panel.hidden = true; return }

  ensureRepBoard().addArrow(ARROW_TYPE.success,
    c.expected_uci.slice(0, 2), c.expected_uci.slice(2, 4))

  const label = {
    material: "That drops material",
    worse: "Playable, but worse",
    transposes: "Same position, different order",
    playable: "Not this line",
  }[c.severity] || "Not this line"

  const used = rep.corrections.length
  panel.innerHTML = `
    <div class="rep-verdict is-miss">${label}</div>
    <p class="rep-explain">${escapeHtml(c.explanation)}</p>
    <p class="rep-should">Take it back and play <b>${escapeHtml(c.expected)}</b>.</p>
    <p class="hint">${used} correction${used === 1 ? "" : "s"} in this run — the
      line banks only when you play it start to finish without one.</p>`
  panel.hidden = false
}

function repMoveInput(event) {
  const board = ensureRepBoard()
  if (event.type === INPUT_EVENT_TYPE.moveInputStarted) {
    const targets = repTargets(event.squareFrom)
    if (!targets.length) return false
    board.addLegalMovesMarkers(targets)
    return true
  }
  if (event.type === INPUT_EVENT_TYPE.moveInputCanceled ||
      event.type === INPUT_EVENT_TYPE.moveInputFinished) {
    board.removeLegalMovesMarkers()
    return true
  }
  if (event.type === INPUT_EVENT_TYPE.validateMoveInput) {
    const plain = event.squareFrom + event.squareTo
    const legal = state.rep.legal_moves
    if (legal.includes(plain)) { submitRepMove(plain); return true }
    const promos = legal.filter((m) => m.startsWith(plain) && m.length === 5)
    if (promos.length) { submitRepMove(plain + "q"); return true }
    return false
  }
  return true
}

function repTargets(from) {
  if (!state.rep || state.rep.status !== "playing") return []
  const seen = new Set()
  const out = []
  for (const uci of state.rep.legal_moves) {
    if (!uci.startsWith(from)) continue
    const to = uci.slice(2, 4)
    if (seen.has(to)) continue
    seen.add(to)
    out.push({from, to, promotion: uci.length === 5 ? uci[4] : undefined})
  }
  return out
}

async function submitRepMove(uci) {
  const previous = state.rep.san.length
  const corrections = state.rep.corrections.length
  ensureRepBoard().disableMoveInput()
  $("rep-status").textContent = "…"
  try {
    const rep = await api(`/api/reps/${state.rep.id}/move`, {
      method: "POST",
      body: JSON.stringify({uci}),
    })
    // A correction makes no move, so there is nothing to sound out — and the
    // moves already on the board must not be replayed as if they were new.
    if (rep.corrections.length > corrections) {
      sound.play("lose", 0)
    } else {
      rep.san.slice(previous).forEach((san, i) => {
        const voice = sound.voiceForSan(san)
        if (voice) sound.play(voice, i * 0.17)
      })
    }
    applyRep(rep)
  } catch (err) {
    $("rep-status").textContent = err.message
    applyRep(state.rep)
  }
}

function showRepResult(rep) {
  const passed = rep.status === "passed"
  sound.play(passed ? "win" : "lose", 0.25)
  $("rep-next").hidden = false
  $("rep-status").textContent = passed
    ? "Correct — you played the whole line."
    : "You finished the line, but with help."

  // Every rep now reaches the end of the line, so the line is always shown in
  // full. The plies marked are the ones you had to be told, which is the thing
  // worth looking at afterwards.
  const marked = rep.line
    .map((san, i) => {
      const helped = rep.corrected_plies.includes(i)
      const yours = rep.your_plies.includes(i)
      if (helped) return `<b class="rep-target">${escapeHtml(san)}</b>`
      return yours ? `<b>${escapeHtml(san)}</b>` : escapeHtml(san)
    })
    .join(" ")

  const n = rep.corrected_plies.length
  let html = `<div class="rep-verdict ${passed ? "is-pass" : "is-miss"}">
      ${passed ? "Line complete" : `Line complete — ${n} correction${n === 1 ? "" : "s"}`}
    </div>
    <p class="rep-line">${marked}</p>
    <p class="hint">Your moves are in bold${
      n ? "; the ones you needed telling are marked" : ""}.</p>`

  const p = rep.progress
  html += `<p class="hint">${
    p.mastered
      ? "Known — this line is in the bank, and a new one is unlocked."
      : passed
        ? `Streak ${p.streak} of ${p.needed} — get it right ${p.needed - p.streak} more time${p.needed - p.streak === 1 ? "" : "s"} to bank it.`
        : `Streak back to ${p.streak} of ${p.needed}. Play it through with no corrections to bank it.`
  }</p>`

  $("rep-result").innerHTML = html
  $("rep-result").hidden = false
  loadRepProgress()
}

/* ------------------------------------------------------------ history -- */

/* Trainer games are played against Maia at a dial setting; imported ones were
   played against a person at a rating. Both belong in the same column. */
function opponentLabel(g) {
  if (g.source === "chesscom") {
    return g.opponent_rating ? `${g.opponent_rating} <span class="src">chess.com</span>` : "chess.com"
  }
  return g.bot_elo ?? "—"
}

async function loadHistory() {
  const rows = await api("/api/games?limit=50")
  const body = document.querySelector("#history-table tbody")
  body.innerHTML = ""
  $("history-empty").hidden = rows.length > 0
  rows.forEach((g) => {
    const won =
      (g.result === "1-0" && g.player_color === "white") ||
      (g.result === "0-1" && g.player_color === "black")
    const cls = !g.result ? "" : g.result === "1/2-1/2" ? "res-draw" : won ? "res-win" : "res-loss"
    const label = !g.result ? "unfinished" : g.result === "1/2-1/2" ? "draw" : won ? "win" : "loss"
    const tr = document.createElement("tr")
    tr.innerHTML =
      `<td>${escapeHtml((g.created_at || "").replace("T", " ").slice(0, 16))}</td>` +
      `<td>${escapeHtml(g.opening || "—")}</td>` +
      `<td>${escapeHtml(g.player_color)}</td>` +
      `<td class="${cls}">${label}</td>` +
      `<td>${opponentLabel(g)}</td>` +
      `<td><button class="linkish">${g.result ? "review" : "resume"}</button></td>`
    const caption = [
      (g.created_at || "").replace("T", " ").slice(0, 16),
      g.opening || "unnamed opening",
      `you were ${g.player_color}`,
    ].join(" · ")
    tr.querySelector("button").addEventListener("click", async () => {
      if (g.result) return startReview(g.id, caption)
      applyGame(await api(`/api/games/${g.id}`))
      $("setup").hidden = true
      $("game-info").hidden = false
      showView("play")
    })
    body.appendChild(tr)
  })
}

/* --------------------------------------------------------------- util -- */

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]))
}
const fmtPct = (v) => (v == null ? "–" : `${v.toFixed(1)}%`)

async function resumeIfAny() {
  try {
    const game = await api("/api/games/current")
    if (game && game.id) {
      applyGame(game)
      $("setup").hidden = true
      $("game-info").hidden = false
      $("g-status").textContent =
        (game.turn === game.player_color ? "Your move." : "Bot is thinking…") +
        " (resumed)"
    }
  } catch (err) {
    console.error("could not resume", err)
  }
}

refreshState().then(resumeIfAny).catch((e) => console.error(e))


/* ------------------------------------------------------- chess.com -- */
/* Your real games, reviewed by the same pipeline as the trainer ones so the
   two land in one metric space. The import is a background job on the server;
   this polls it rather than holding a request open for several minutes. */

async function loadChessCom() {
  let d
  try {
    d = await api("/api/chesscom")
  } catch (err) {
    return
  }
  state.chesscom = d
  const linked = Boolean(d.username)
  $("cc-connect").hidden = linked
  $("cc-linked").hidden = !linked
  if (linked) {
    $("cc-name").textContent = d.username
    $("cc-counts").textContent = d.games
      ? `· ${d.games} game${d.games === 1 ? "" : "s"} imported, ${d.reviewed} reviewed`
      : "· nothing imported yet"
  }
  showImportProgress(d.progress)
  showWatch(d.watch)
  if (d.progress?.running) setTimeout(() => loadChessCom().catch(() => {}), 2000)
}

/* The watcher runs on the server whether this page is open or not, so this is
   a report on it, not a control loop. It has to show when it last looked —
   "watching" with no timestamp is indistinguishable from a dead task. */
function showWatch(w) {
  if (!w) return
  $("cc-auto").checked = w.enabled
  if (!w.enabled) {
    $("cc-watch").textContent = "Not watching — new games arrive when you press Import."
    return
  }
  const every = w.every >= 60
    ? `${Math.round(w.every / 60)} min`
    : `${w.every}s`
  const bits = [`Watching every ${every}`]
  if (w.checked_at) bits.push(`last looked ${sinceText(w.checked_at)}`)
  else bits.push("first check due shortly")
  if (w.new_games) bits.push(`${w.new_games} brought in since restart`)
  if (w.error) bits.push(w.error)
  $("cc-watch").textContent = bits.join(" · ")
}

function sinceText(iso) {
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  if (seconds < 90) return "just now"
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  return `${Math.round(seconds / 3600)} h ago`
}

function showImportProgress(p) {
  const el = $("cc-progress")
  if (!p || p.step === "idle") { el.textContent = ""; return }
  if (p.error) {
    el.textContent = `Import failed: ${p.error}`
    return
  }
  const bits = [p.step]
  if (p.imported) bits.push(`${p.imported} imported`)
  if (p.skipped) bits.push(`${p.skipped} already had`)
  if (p.to_review) bits.push(`${p.reviewed}/${p.to_review} reviewed`)
  el.textContent = bits.join(" · ")
  if (p.step === "done" && !p.running) {
    loadProgress().catch(() => {})
    loadHistory().catch(() => {})
  }
}

/* The filter is only meaningful if it says how much is behind each option —
   "chess.com" reading 0 reviewed explains an empty panel that would otherwise
   look broken. */
function labelStatsScopes(sources) {
  if (!sources) return
  const label = {
    all: "All games",
    trainer: "Trainer",
    chesscom: "chess.com",
  }
  document.querySelectorAll("#stats-scope .chip").forEach((chip) => {
    const key = chip.dataset.source
    const counted = key === "all"
      ? sources.trainer.reviewed + sources.chesscom.reviewed
      : sources[key]?.reviewed
    chip.textContent = counted == null ? label[key] : `${label[key]} (${counted})`
  })
}

function ccError(message) {
  const el = $("cc-error")
  el.textContent = message || ""
  el.hidden = !message
}

$("cc-connect-btn").addEventListener("click", async () => {
  const username = $("cc-username").value.trim()
  if (!username) return
  ccError("")
  $("cc-connect-btn").disabled = true
  try {
    await api("/api/chesscom/connect", {
      method: "POST",
      body: JSON.stringify({username}),
    })
    await loadChessCom()
  } catch (err) {
    ccError(err.message)
  } finally {
    $("cc-connect-btn").disabled = false
  }
})

$("cc-username").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("cc-connect-btn").click()
})

$("cc-import").addEventListener("click", async () => {
  ccError("")
  $("cc-import").disabled = true
  try {
    // 20 is the review budget, not an import limit: every game is fetched,
    // the newest 20 unreviewed ones are analysed now, the rest on demand.
    await api("/api/chesscom/import", {
      method: "POST",
      body: JSON.stringify({review: 20, time_classes: ["rapid"]}),
    })
    await loadChessCom()
  } catch (err) {
    ccError(err.message)
  } finally {
    $("cc-import").disabled = false
  }
})

$("cc-auto").addEventListener("change", async () => {
  await saveSetting({auto_import: $("cc-auto").checked})
  await loadChessCom()
})

$("cc-forget").addEventListener("click", async () => {
  await api("/api/chesscom/connect", {method: "DELETE"})
  await loadChessCom()
})

document.querySelectorAll("#stats-scope .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    state.statsScope = chip.dataset.source
    document.querySelectorAll("#stats-scope .chip")
      .forEach((c) => c.classList.toggle("is-on", c === chip))
    loadProgress().catch((e) => console.error(e))
  })
})

/* --------------------------------------------------------- puzzles -- */
/* Drawn from whatever is costing the most games right now. The theme is never
   shown before the attempt: naming it would hand over the answer, and for the
   seven motifs whose mapping is inverted it would also be a lie — a fork puzzle
   trains you to play forks, not to avoid them. It appears afterwards instead,
   where it reads as feedback. */

let puzzleBoard = null

function ensurePuzzleBoard() {
  if (!puzzleBoard) puzzleBoard = makeBoard($("puzzleboard"))
  return puzzleBoard
}

async function loadPuzzles() {
  let d
  try {
    d = await api("/api/puzzles")
  } catch (err) {
    return
  }
  state.puzzleStatus = d
  $("pz-empty").hidden = d.ready
  $("pz-card").hidden = !d.ready
  $("pz-scores").hidden = !d.ready
  if (!d.ready) return
  renderPuzzleScores(d.scores)
  loadBookmarks().catch(() => {})
  if (!state.puzzle) await nextPuzzle()
}

function renderPuzzleScores(scores) {
  const rows = Object.entries(scores || {})
  $("pz-scores").hidden = false
  $("pz-score-list").innerHTML = rows.length
    ? rows
        .sort((a, b) => b[1].tried - a[1].tried)
        .map(([motif, s]) =>
          `<li><span class="rep-nm">${escapeHtml(motifLabel(motif))}</span>
             <span class="rep-streak">${s.solved}/${s.tried}</span></li>`)
        .join("")
    : '<li><span class="hint">Nothing solved yet.</span></li>'
}

function motifLabel(motif) {
  return (motif || "").replace(/_/g, " ")
}

async function nextPuzzle() {
  $("pz-next").disabled = true
  $("pz-result").hidden = true
  try {
    applyPuzzle(await api("/api/puzzles/next", {method: "POST"}))
  } catch (err) {
    $("pz-status").textContent = err.message
  } finally {
    $("pz-next").disabled = false
  }
}

function applyPuzzle(p, keepDisclosures) {
  const fresh = !keepDisclosures && state.puzzle?.id !== p.id
  state.puzzle = p
  const board = ensurePuzzleBoard()
  board.setPosition(p.fen, true)
  board.setOrientation(p.you_play === "black" ? COLOR.black : COLOR.white)
  board.removeArrows()
  board.removeMarkers()
  board.removeLegalMovesMarkers()   // the last move leaves its own behind

  if (fresh) {
    // Everything you can ask for is asked for again on a new puzzle: a hint
    // that stayed revealed would answer the next one for free.
    state.hintLevel = 0
    state.replayPly = null
    showDisclosure("pz-show-length", "pz-progress", false)
    showDisclosure("pz-show-type", "pz-type", false)
    $("pz-replay").hidden = true
    $("pz-result").hidden = true
    $("pz-after").hidden = true
  }
  $("pz-hint").textContent = state.hintLevel === 0 ? "Hint" : "Where to?"
  $("pz-hint").hidden = p.status !== "playing" || state.hintLevel >= 2
  setBookmarkButton(p.bookmarked)

  $("pz-side").textContent = p.you_play
  $("pz-progress").textContent = `${p.moves_found} of ${p.moves_total}`

  if (p.status === "playing") {
    $("pz-title").textContent = "Find the best move"
    $("pz-status").textContent = p.last_move_wrong
      ? "Not that one — try again."
      : p.wrong > 0
        ? "Keep going."
        : "Your move."
    setMoveInput(board, puzzleMoveInput, p.you_play === "black" ? COLOR.black : COLOR.white)
  } else {
    board.disableMoveInput()
    showPuzzleResult(p)
  }
}

function showPuzzleResult(p) {
  const clean = p.status === "solved"
  sound.play(clean ? "win" : "lose", 0.2)
  $("pz-title").textContent = clean ? "Solved" : "Puzzle over"
  $("pz-status").textContent = ""

  // Only now is the theme named — as feedback, not as a hint.
  $("pz-result").innerHTML = `
    <div class="rep-verdict ${clean ? "is-pass" : "is-miss"}">
      ${clean ? "Solved first time" : p.status === "gave_up" ? "Shown" : `Solved, ${p.wrong} wrong turn${p.wrong === 1 ? "" : "s"}`}
    </div>
    <p class="rep-explain">${escapeHtml(p.note || "")}</p>
    <p class="hint">Chosen because <b>${escapeHtml(motifLabel(p.motif))}</b> is
      one of your commonest mistakes${p.direction === "inverted"
        ? " — this is that pattern from the winning side" : ""}.
      Solution: <b>${(p.solution || []).join(" ")}</b>${
        p.rating ? ` · lichess rating ${p.rating}` : ""}</p>`
  $("pz-result").hidden = false
  showDisclosure("pz-show-length", "pz-progress", true)
  showDisclosure("pz-show-type", "pz-type", true)
  $("pz-type").textContent = motifLabel(p.motif)
  loadPuzzles().catch(() => {})

  showContinuation(p).catch((e) => console.error(e))

  if (!p.line?.length) return
  if (p.status === "gave_up") {
    // The solution was asked for, so play it out.
    animateSolution(p).catch((e) => console.error(e))
  } else {
    // It was found. You have just watched these moves go in one at a time;
    // replaying them is time you did not ask for. Park the stepper at the end
    // instead, so rewinding is there if you want to look again.
    state.replayPly = p.line.length - 1
    $("pz-replay").hidden = false
    renderPly()
  }
}

/* Length and kind are both hidden until asked for. "Two moves to find" is a
   real clue — it rules out every one-move answer — and so is "this is a
   back-rank mate". Offered rather than shown, and both count as a hint. */
function showDisclosure(buttonId, valueId, revealed) {
  $(buttonId).hidden = revealed
  $(valueId).hidden = !revealed
}

function setBookmarkButton(on) {
  const b = $("pz-bookmark")
  b.textContent = on ? "★ Kept" : "☆ Keep"
  b.setAttribute("aria-pressed", on ? "true" : "false")
}

/* Walk the solution slowly enough to watch. Each position comes from the
   server, so stepping needs no chess rules here. */
async function animateSolution(p, msPerPly = 900) {
  const board = ensurePuzzleBoard()
  board.removeMarkers()
  board.setPosition(p.start_fen, true)
  await new Promise((r) => setTimeout(r, 450))
  for (const step of p.line) {
    board.setPosition(step.fen, true)
    board.removeMarkers()
    board.addMarker(MARKER_TYPE.framePrimary, step.uci.slice(0, 2))
    board.addMarker(MARKER_TYPE.framePrimary, step.uci.slice(2, 4))
    $("pz-ply-label").textContent = `${step.yours ? "you" : "they"} play ${step.san}`
    await new Promise((r) => setTimeout(r, msPerPly))
  }
  state.replayPly = p.line.length - 1
  $("pz-replay").hidden = false
  renderPly()
}

function renderPly() {
  const p = state.puzzle
  if (!p?.line) return
  const i = state.replayPly
  const board = ensurePuzzleBoard()
  board.removeMarkers()
  if (i < 0) {
    board.setPosition(p.start_fen, true)
    $("pz-ply-label").textContent = "the position you were given"
  } else {
    const step = p.line[i]
    board.setPosition(step.fen, true)
    board.addMarker(MARKER_TYPE.framePrimary, step.uci.slice(0, 2))
    board.addMarker(MARKER_TYPE.framePrimary, step.uci.slice(2, 4))
    $("pz-ply-label").textContent =
      `${i + 1} of ${p.line.length} — ${step.yours ? "you" : "they"} play ${step.san}`
  }
  $("pz-prev").disabled = i < 0
  $("pz-next-ply").disabled = i >= p.line.length - 1
}

/* A puzzle stops the moment the tactic is won, which is exactly where the
   interesting question starts at this level: you have won a rook, now what?
   Fetched after the result is on screen, so waiting for the engine never
   delays the verdict. */
async function showContinuation(p) {
  const el = $("pz-after")
  el.hidden = false
  el.innerHTML = `<p class="hint">Looking at what happens next…</p>`
  let c
  try {
    c = await api(`/api/puzzles/${p.id}/continuation`, {method: "POST"})
  } catch (err) {
    el.hidden = true
    return
  }
  if (state.puzzle?.id !== p.id) return      // they moved on while we waited

  if (c.over) {
    el.innerHTML = `<p class="hint">${escapeHtml(c.note)}</p>`
    return
  }
  const verdict = c.mate != null
    ? `<b>M${Math.abs(c.mate)}</b> ${c.mate > 0 ? "for you" : "against you"}`
    : `<b>${c.cp > 0 ? "+" : ""}${(c.cp / 100).toFixed(1)}</b> ${
        c.cp > 0 ? "for you" : "against you"}`
  el.innerHTML = `
    <h3>What happens next</h3>
    <p class="pz-after-eval">${verdict} · ${c.yours ? "your move" : "their move"}</p>
    <p class="rep-line">${escapeHtml(c.line.join(" "))}</p>
    <p class="hint">Stockfish to depth ${c.depth}, from where the puzzle stopped.</p>`
}

function puzzleMoveInput(event) {
  const board = ensurePuzzleBoard()
  if (event.type === INPUT_EVENT_TYPE.moveInputStarted) {
    const targets = puzzleTargets(event.squareFrom)
    if (!targets.length) return false
    board.addLegalMovesMarkers(targets)
    return true
  }
  if (event.type === INPUT_EVENT_TYPE.moveInputCanceled ||
      event.type === INPUT_EVENT_TYPE.moveInputFinished) {
    board.removeLegalMovesMarkers()
    return true
  }
  if (event.type === INPUT_EVENT_TYPE.validateMoveInput) {
    const plain = event.squareFrom + event.squareTo
    const legal = state.puzzle.legal_moves
    if (legal.includes(plain)) { submitPuzzleMove(plain); return true }
    const promos = legal.filter((m) => m.startsWith(plain) && m.length === 5)
    if (promos.length) { submitPuzzleMove(plain + "q"); return true }
    return false
  }
  return true
}

function puzzleTargets(from) {
  if (!state.puzzle || state.puzzle.status !== "playing") return []
  const seen = new Set()
  const out = []
  for (const uci of state.puzzle.legal_moves) {
    if (!uci.startsWith(from)) continue
    const to = uci.slice(2, 4)
    if (seen.has(to)) continue
    seen.add(to)
    out.push({from, to, promotion: uci.length === 5 ? uci[4] : undefined})
  }
  return out
}

async function submitPuzzleMove(uci) {
  ensurePuzzleBoard().disableMoveInput()
  try {
    const p = await api(`/api/puzzles/${state.puzzle.id}/move`, {
      method: "POST",
      body: JSON.stringify({uci}),
    })
    sound.play(p.last_move_wrong ? "lose" : "move", 0)
    applyPuzzle(p)
  } catch (err) {
    $("pz-status").textContent = err.message
    applyPuzzle(state.puzzle)
  }
}

$("pz-next").addEventListener("click", () => { state.puzzle = null; nextPuzzle() })
$("pz-giveup").addEventListener("click", async () => {
  if (!state.puzzle) return
  applyPuzzle(await api(`/api/puzzles/${state.puzzle.id}/give_up`, {method: "POST"}))
})

/* Hint in two taps: which piece, then where it goes. "Which piece" is most of
   the work at this level, and being handed the whole move teaches nothing. */
$("pz-hint").addEventListener("click", async () => {
  if (!state.puzzle || state.puzzle.status !== "playing") return
  const level = (state.hintLevel || 0) + 1
  try {
    const h = await api(`/api/puzzles/${state.puzzle.id}/hint?level=${level}`,
                        {method: "POST"})
    const board = ensurePuzzleBoard()
    board.removeMarkers()
    board.addMarker(MARKER_TYPE.framePrimary, h.squares[0])
    if (h.squares[1]) board.addMarker(MARKER_TYPE.circlePrimary, h.squares[1])
    state.hintLevel = h.level
    state.puzzle.hints = h.hints
    $("pz-hint").textContent = "Where to?"
    $("pz-hint").hidden = h.level >= 2
    $("pz-status").textContent = h.level === 1
      ? "That is the piece to move."
      : "That is the move."
  } catch (err) {
    $("pz-status").textContent = err.message
  }
})

$("pz-show-length").addEventListener("click", () => {
  showDisclosure("pz-show-length", "pz-progress", true)
})

$("pz-show-type").addEventListener("click", async () => {
  if (!state.puzzle) return
  try {
    const t = await api(`/api/puzzles/${state.puzzle.id}/reveal_type`, {method: "POST"})
    $("pz-type").textContent = motifLabel(t.motif)
    showDisclosure("pz-show-type", "pz-type", true)
    $("pz-status").textContent = t.note || ""
  } catch (err) {
    $("pz-status").textContent = err.message
  }
})

$("pz-bookmark").addEventListener("click", async () => {
  if (!state.puzzle) return
  const on = !state.puzzle.bookmarked
  await api(`/api/puzzles/${state.puzzle.id}/bookmark`, {
    method: "POST", body: JSON.stringify({on}),
  })
  state.puzzle.bookmarked = on
  setBookmarkButton(on)
  loadBookmarks().catch(() => {})
})

$("pz-first").addEventListener("click", () => { state.replayPly = -1; renderPly() })
$("pz-prev").addEventListener("click", () => {
  state.replayPly = Math.max(-1, (state.replayPly ?? 0) - 1); renderPly()
})
$("pz-next-ply").addEventListener("click", () => {
  const n = state.puzzle?.line?.length ?? 0
  state.replayPly = Math.min(n - 1, (state.replayPly ?? -1) + 1); renderPly()
})
$("pz-replay-again").addEventListener("click", () => {
  if (state.puzzle?.line) animateSolution(state.puzzle).catch(() => {})
})

async function loadBookmarks() {
  const rows = await api("/api/puzzles/bookmarks")
  $("pz-bookmarks").hidden = rows.length === 0
  $("pz-bookmark-list").innerHTML = rows
    .map((b) => {
      const last = b.last
        ? (b.last.solved ? "solved" : "missed") +
          (b.last.hints ? ` · ${b.last.hints} hint${b.last.hints === 1 ? "" : "s"}` : "")
        : "not tried"
      return `<li data-id="${escapeHtml(b.id)}" tabindex="0" role="button">
          <span class="rep-nm">${escapeHtml(motifLabel(b.motif) || "puzzle")}</span>
          <span class="rep-streak">${escapeHtml(last)}</span>
          <span class="rep-mv">${b.rating ? `lichess ${b.rating}` : ""}</span>
        </li>`
    })
    .join("")
}

$("pz-bookmark-list").addEventListener("click", async (e) => {
  const row = e.target.closest("li[data-id]")
  if (!row) return
  state.puzzle = null
  applyPuzzle(await api(`/api/puzzles/${row.dataset.id}/retry`, {method: "POST"}))
})
