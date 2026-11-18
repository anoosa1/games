#!/usr/bin/env python3
"""Run a single, in-memory Ludo table. Use --tailcat to invite friends."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import subprocess
from threading import Lock, Thread


COLORS = ("red", "green", "yellow", "blue")
SEAT_ORDER = (2, 1, 3)  # Seat the first guest opposite the host.
HOME = 56
LANE_START = 51


def rotate(square: tuple[int, int], turns: int) -> tuple[int, int]:
    row, col = square
    for _ in range(turns):
        row, col = col, 14 - row
    return row, col


# Rotate one quarter of the board to keep the four routes symmetrical.
QUARTER = (
    (6, 1), (6, 2), (6, 3), (6, 4), (6, 5), (5, 6), (4, 6),
    (3, 6), (2, 6), (1, 6), (0, 6), (0, 7), (0, 8),
)
TRACK = tuple(rotate(square, seat) for seat in range(4) for square in QUARTER)
SAFE = frozenset((13 * seat + offset) for seat in range(4) for offset in (0, 8))
LANES = tuple(
    tuple(rotate((7, col), seat) for col in range(1, 7)) for seat in range(4)
)
YARDS = tuple(
    tuple(rotate(square, seat) for square in ((1, 1), (1, 4), (4, 1), (4, 4)))
    for seat in range(4)
)
BOARD_LAYOUT = {
    "colors": COLORS, "track": TRACK, "safe": sorted(SAFE),
    "lanes": LANES, "yards": YARDS, "laneStart": LANE_START, "home": HOME,
}


class GameError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


@dataclass
class Player:
    name: str
    token: str
    pieces: list[int] = field(default_factory=lambda: [-1] * 4)


class Game:
    """Own the table state. HTTP handlers hold the lock for each operation."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.host_token = secrets.token_hex(16)
        self.port = 0
        self.invite = ""
        self.tailcat_starting = False
        self.players: list[Player | None] = [None] * 4
        self.version = 0
        self.phase = "lobby"
        self.turn = 0
        self.die = 0
        self.rolled_by: int | None = None
        self.moves: list[int] = []
        self.message = "Waiting for players."

    def seat_for(self, token: str) -> int | None:
        for seat, player in enumerate(self.players):
            if player is not None and token == player.token:
                return seat
        return None

    def view(self, token: str) -> dict:
        return {
            "version": self.version,
            "phase": self.phase,
            "players": [
                None if player is None else {"name": player.name, "pieces": player.pieces.copy()}
                for player in self.players
            ],
            "you": self.seat_for(token),
            "host": token == self.host_token,
            "invite": self.invite if token == self.host_token else "",
            "tailcatStarting": self.tailcat_starting and token == self.host_token,
            "port": self.port,
            "turn": self.turn,
            "die": self.die,
            "rolledBy": self.rolled_by,
            "moves": self.moves.copy(),
            "message": self.message,
        }

    def join(self, token: str, name: object) -> None:
        if self.seat_for(token) is not None:
            return
        if self.phase != "lobby":
            raise GameError("A match is in progress. Ask the host to reopen the lobby.")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 20:
            raise GameError("Enter a name between 1 and 20 characters.")
        if not name.isprintable():
            raise GameError("Use printable characters in your name.")
        seats = (0,) if token == self.host_token else SEAT_ORDER
        seat = next((seat for seat in seats if self.players[seat] is None), None)
        if seat is None:
            raise GameError("The table is full.")
        self.players[seat] = Player(name.strip(), token)
        self.version += 1

    def action(self, token: str, data: dict) -> None:
        action = data.get("action")
        if action == "join":
            self.join(token, data.get("name"))
            return
        seat = self.seat_for(token)
        if seat is None:
            raise GameError("Take a seat to play.", HTTPStatus.FORBIDDEN)
        version = data.get("version")
        if type(version) is not int or version != self.version:
            raise GameError("The table updated. Try again.", HTTPStatus.CONFLICT)

        if action in ("start", "lobby", "remove"):
            if seat != 0:
                raise GameError("Only the host can do that.", HTTPStatus.FORBIDDEN)
            self.host_action(action, data.get("seat"))
        elif action == "leave" and self.phase == "lobby" and seat != 0:
            self.players[seat] = None
        elif action in ("roll", "move"):
            if self.phase != "playing" or seat != self.turn:
                raise GameError("Wait for your turn.")
            if action == "roll":
                self.roll(seat)
            else:
                self.move(seat, data.get("piece"))
        else:
            raise GameError("That action is unavailable.")
        self.version += 1

    def host_action(self, action: str, target: object) -> None:
        if action == "remove":
            if self.phase != "lobby" or type(target) is not int or target not in SEAT_ORDER:
                raise GameError("Choose a guest seat in the lobby.")
            self.players[target] = None
            return
        if action == "start":
            if self.phase not in ("lobby", "won"):
                raise GameError("A match is already in progress.")
            if sum(player is not None for player in self.players) < 2:
                raise GameError("At least two players are needed.")
            self.phase = "playing"
            self.message = f"{self.player(0).name} goes first."
        else:
            self.phase = "lobby"
            self.message = "Waiting for players."
        for player in self.players:
            if player is not None:
                player.pieces = [-1] * 4
        self.turn = self.die = 0
        self.rolled_by = None
        self.moves = []

    def player(self, seat: int) -> Player:
        player = self.players[seat]
        assert player is not None
        return player

    def next_turn(self) -> None:
        for offset in range(1, 4):
            seat = (self.turn + offset) % 4
            if self.players[seat] is not None:
                self.turn = seat
                return

    def roll(self, seat: int) -> None:
        if self.moves:
            raise GameError("Move a piece before rolling again.")
        player = self.player(seat)
        self.die = secrets.randbelow(6) + 1
        self.rolled_by = seat
        self.moves = [
            index for index, step in enumerate(player.pieces)
            if (step == -1 and self.die == 6) or (0 <= step <= HOME - self.die)
        ]
        self.message = f"{player.name} rolled {self.die}."
        if not self.moves:
            if self.die == 6:
                self.message += " No legal move. Roll again."
            else:
                self.message += " No legal move."
                self.next_turn()

    def move(self, seat: int, piece: object) -> None:
        if type(piece) is not int or piece not in self.moves:
            raise GameError("Choose a highlighted piece.")
        player = self.player(seat)
        step = player.pieces[piece]
        step = 0 if step == -1 else step + self.die
        player.pieces[piece] = step
        self.moves = []

        captures = []
        if step < LANE_START:
            square = (13 * seat + step) % len(TRACK)
            if square not in SAFE:
                for other_seat, other in enumerate(self.players):
                    if other is None or other_seat == seat:
                        continue
                    for index, other_step in enumerate(other.pieces):
                        if 0 <= other_step < LANE_START and (13 * other_seat + other_step) % len(TRACK) == square:
                            other.pieces[index] = -1
                            captures.append(other.name)

        if all(position == HOME for position in player.pieces):
            self.phase = "won"
            self.message = f"{player.name} brought all four pieces home."
        elif captures:
            self.message = f"{player.name} sent {', '.join(dict.fromkeys(captures))} back to their yard. Roll again."
        else:
            self.message = (
                f"{player.name} brought a piece home." if step == HOME
                else f"{player.name} moved piece {piece + 1}."
            )
            if self.die == 6:
                self.message += " Roll again."
            else:
                self.next_turn()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f3e5d1">
<title>Ludo · Tabletop</title>
<style>
  :root {
    color-scheme: light;
    --page: #f3e5d1; --paper: #fbf1e3; --ink: #30251d; --muted: #6d5848;
    --line: #cbb491; --accent: #9e392b; --track: #fffaf0; --grid: #c7b69e;
    --red: #d75343; --green: #328a62; --yellow: #e5a82e; --blue: #428acb;
  }
  * { box-sizing: border-box }
  [hidden] { display: none !important }
  body { margin: 0; background: var(--page); color: var(--ink); font: 16px/1.45 system-ui, sans-serif }
  main { width: min(100% - 48px, 1000px); margin: 0 auto; padding: 35px 0 38px }
  header { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding-bottom: 20px; border-bottom: 1px solid var(--line); margin-bottom: 29px }
  h1, h2 { font-family: Georgia, serif; margin: 0; line-height: 1.05 }
  h1 { font-size: clamp(54px, 8vw, 82px); letter-spacing: -.075em }
  h2 { font-size: 30px; letter-spacing: -.034em }
  p { margin: 0 }
  .header-actions, .actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap }
  button, input { font: inherit }
  button { cursor: pointer; font-weight: 650 }
  button:focus-visible, input:focus-visible, summary:focus-visible { outline: 3px solid #225487; outline-offset: 3px }
  button:disabled { cursor: default }
  .outline, .primary, .quiet { border-radius: 8px; padding: 8px 13px; font-size: 14px; min-height: 40px }
  .outline { color: var(--ink); border: 1px solid #b69d7a; background: transparent }
  .outline:hover, .quiet:hover { background: #eddfca }
  .primary { border: 1px solid var(--accent); color: #fffaf2; background: var(--accent) }
  .primary:hover:not(:disabled) { background: #842d21 }
  .primary:disabled { color: #765d4c; border-color: #e2ceb3; background: #eddfca }
  .quiet { color: var(--muted); background: transparent; border: 1px solid transparent }
  .layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(246px, 310px); gap: 29px; align-items: center }
  .board { position: relative; aspect-ratio: 1; background: #f9eee0; border: 1px solid #b8a180; border-radius: 10px; outline: 5px solid #e6d2b5; box-shadow: 0 5px 19px #5b391c21 }
  .layer { position: absolute; inset: 2%; display: grid; grid-template: repeat(15, minmax(0, 1fr)) / repeat(15, minmax(0, 1fr)) }
  .tile { display: grid; place-items: center; background: var(--track); border: 1px solid var(--grid); margin: 0 -1px -1px 0; font-size: clamp(9px, 2.2vw, 18px); font-weight: 750; line-height: 1 }
  .red { --team: var(--red); --team-ink: #822e25 }
  .green { --team: var(--green); --team-ink: #12432e }
  .yellow { --team: var(--yellow); --team-ink: #694304 }
  .blue { --team: var(--blue); --team-ink: #183f6c }
  .lane, .yard { background: var(--team) }
  .lane { color: #25211c }
  .yard { display: grid; place-items: center; border: 1px solid var(--team-ink) }
  .yard-inner { width: 68%; height: 68%; border-radius: 8px; border: 1px solid var(--team-ink); background: color-mix(in srgb, var(--team) 24%, var(--paper)) }
  .yard.inactive { opacity: .47 }
  .center { background: conic-gradient(from 45deg, var(--yellow) 0 25%, var(--blue) 25% 50%, var(--red) 50% 75%, var(--green) 75%); outline: 1px solid var(--grid) }
  .piece-layer { pointer-events: none }
  .stack { display: grid; grid-template-columns: repeat(var(--columns, 2), minmax(0, 1fr)); grid-auto-rows: minmax(0, 1fr); gap: 1px; padding: 1px }
  .piece { display: grid; place-items: center; align-self: center; justify-self: center; width: 92%; aspect-ratio: 1; padding: 0; border: 1px solid var(--team-ink); border-radius: 50%; color: #211b16; background: var(--team); box-shadow: 0 1px 0 1px var(--team-ink), inset 0 0 0 2px #ffffff70; line-height: 1; font-size: clamp(8px, 1.7vw, 14px); pointer-events: auto }
  .stack .piece { width: 100%; font-size: 0; box-shadow: inset 0 0 0 1px #ffffff70 }
  .piece:enabled { outline: 2px solid #281d14; outline-offset: 1px }
  .piece:enabled:hover, .piece:focus-visible { transform: scale(1.12); outline: 2px solid #225487; z-index: 1 }
  .piece.empty { opacity: .48; pointer-events: none }
  .panel { background: var(--paper); border: 1px solid var(--line); border-radius: 14px; padding: 22px }
  .message, .network, .subtle { color: var(--muted); font-size: 14px }
  .message { margin-top: 6px; min-height: 2.9em; overflow-wrap: anywhere }
  .network { margin-top: 12px; color: #8c3a29 }
  .actions { margin: 14px 0 0 }
  .dice-controls { display: flex; gap: 10px; align-items: center; width: 100% }
  .dice-controls .primary { flex: 1 }
  .die { width: 43px; height: 43px; display: grid; place-items: center; flex: 0 0 auto; border: 1px solid #cdb99e; border-radius: 9px; background: #fffaf0; font-size: 36px; line-height: 1 }
  .players { list-style: none; margin: 17px 0 0; padding: 13px 0 0; border-top: 1px solid #e1cdb2; display: grid; gap: 6px }
  .player { display: flex; align-items: center; gap: 8px; min-height: 30px; border-radius: 6px; padding: 4px 6px; font-size: 14px }
  .player.active { background: #efe0c8 }
  .marker { width: 13px; height: 13px; flex: 0 0 auto; border: 1px solid var(--team-ink); background: var(--team); border-radius: 50% }
  .player-name { min-width: 0; overflow-wrap: anywhere }
  .player.empty { color: var(--muted) }
  .player.empty .marker { background: transparent }
  .score { margin-left: auto; color: var(--muted); font-size: 13px; white-space: nowrap }
  .remove { margin-left: auto; padding: 4px 7px; min-height: 30px; font-size: 12px }
  label { display: block; font-size: 14px; font-weight: 650; margin-bottom: 5px }
  form { width: 100% }
  input { color: var(--ink); background: #fffaf1; border: 1px solid #b69d7a; border-radius: 8px; padding: 8px 10px; min-height: 40px; width: 100% }
  form .primary { width: 100%; margin-top: 8px }
  dialog { width: min(100% - 32px, 470px); color: var(--ink); background: var(--paper); border: 1px solid var(--line); border-radius: 14px; padding: 23px }
  dialog::backdrop { background: #32241c84 }
  dialog h2 { margin: 0 0 12px }
  dialog p + p { margin-top: 9px }
  dialog ul { padding-left: 21px }
  dialog li + li { margin-top: 6px }
  dialog .actions { justify-content: flex-end }
  dialog input { font: 13px ui-monospace, monospace; margin: 10px 0 0 }
  code { font-size: .91em; overflow-wrap: anywhere }
  @media (max-width: 680px) {
    main { width: min(100% - 32px, 480px); padding: 20px 0 30px }
    header { padding-bottom: 13px; margin-bottom: 20px }
    .layout { grid-template-columns: minmax(0, 1fr); gap: 20px }
    .panel { padding: 17px }
    .outline { padding-inline: 10px }
  }
</style>
</head>
<body>
<main>
  <header>
    <h1>Ludo</h1>
    <div class="header-actions">
      <button class="outline" id="rules-button" type="button">How to play</button>
      <button class="outline" id="invite-button" type="button" hidden>Invite players</button>
    </div>
  </header>
  <div class="layout">
    <section class="board" aria-label="Ludo board">
      <div class="layer" id="tiles" aria-hidden="true"></div>
      <div class="layer piece-layer" id="pieces"></div>
    </section>
    <section class="panel" aria-labelledby="turn">
      <h2 id="turn">Ludo table</h2>
      <p class="message" id="message" role="status">Connecting to the table…</p>
      <p class="network" id="error" role="alert" hidden></p>
      <div class="actions">
        <form id="join-form" hidden>
          <label for="name">Your name</label>
          <input id="name" name="name" maxlength="20" required autocomplete="nickname">
          <button class="primary" type="submit">Take a seat</button>
        </form>
        <div class="dice-controls" id="dice-controls" hidden>
          <span class="die" id="die" role="img" aria-label="Die has not been rolled">–</span>
          <button class="primary" id="roll" type="button">Roll dice</button>
        </div>
        <div class="actions" id="move-options" aria-label="Choose a piece" hidden></div>
        <button class="primary" id="start" type="button" hidden>Start match</button>
        <button class="quiet" id="lobby" type="button" hidden>Return to lobby</button>
        <button class="quiet" id="leave" type="button" hidden>Leave seat</button>
      </div>
      <ul class="players" id="players" aria-label="Players"></ul>
    </section>
  </div>
</main>
<dialog id="rules" aria-labelledby="rules-title">
  <h2 id="rules-title">How to play</h2>
  <ul>
    <li>Roll a six to bring a piece out of your yard. Otherwise, move a piece already on the board.</li>
    <li>Land on an opponent’s piece to send it back to its yard. Marked squares are safe.</li>
    <li>Roll again after a six or a capture.</li>
    <li>Use an exact roll to reach the end of your home lane. Get all four pieces home to win.</li>
  </ul>
  <form method="dialog" class="actions"><button class="outline">Close</button></form>
</dialog>
<dialog id="invite" aria-labelledby="invite-title">
  <h2 id="invite-title">Invite players</h2>
  <p id="invite-message"></p>
  <input id="invite-command" aria-label="Tailcat join command" readonly hidden>
  <div class="actions">
    <button class="primary" id="copy" type="button" hidden>Copy command</button>
    <form method="dialog"><button class="outline">Close</button></form>
  </div>
</dialog>
<script>
  const board = __BOARD_LAYOUT__;
  const byId = id => document.getElementById(id);
  const rulesDialog = byId('rules');
  const inviteDialog = byId('invite');
  const players = byId('players');
  const pieces = byId('pieces');
  const tiles = byId('tiles');
  const dieFaces = ['–', '⚀', '⚁', '⚂', '⚃', '⚄', '⚅'];

  const hostToken = new URLSearchParams(location.hash.slice(1)).get('host');
  if (hostToken) history.replaceState(null, '', location.pathname);
  const token = hostToken || sessionStorage.getItem('ludo-seat') || crypto.randomUUID().replaceAll('-', '');
  sessionStorage.setItem('ludo-seat', token);

  let state = null;
  let pending = false;
  let connected = false;

  function place(element, [row, col], span = 1) {
    element.style.gridArea = `${row + 1} / ${col + 1} / span ${span} / span ${span}`;
  }

  function tile(css, square, span, label = '') {
    const element = document.createElement('div');
    element.className = css;
    element.textContent = label;
    place(element, square, span);
    tiles.append(element);
    return element;
  }

  const yards = [[0, 0], [0, 9], [9, 9], [9, 0]].map((square, seat) => {
    const yard = tile('yard ' + board.colors[seat], square, 6);
    const inset = document.createElement('div');
    inset.className = 'yard-inner';
    yard.append(inset);
    return yard;
  });
  board.track.forEach((square, index) => {
    const color = index % 13 === 0 ? ' lane ' + board.colors[index / 13] : '';
    tile('tile' + color, square, 1, board.safe.includes(index) ? '✦' : '');
  });
  board.lanes.forEach((lane, seat) => {
    lane.slice(0, -1).forEach(square => tile('tile lane ' + board.colors[seat], square, 1));
  });
  tile('center', [6, 6], 3);

  // Cells group pieces that share a square, including on safe squares and at home.
  function pieceSquare(seat, index, step) {
    if (step === -1) return board.yards[seat][index];
    if (step >= board.laneStart) return board.lanes[seat][step - board.laneStart];
    return board.track[(13 * seat + step) % board.track.length];
  }

  function renderPieces(canMove) {
    const focused = document.activeElement?.dataset.piece;
    const cells = new Map();
    const fragment = document.createDocumentFragment();
    const rows = state?.players ?? Array(4).fill(null);
    rows.forEach((player, seat) => {
      yards[seat].classList.toggle('inactive', !!state && !player);
      if (!player && state?.phase !== 'lobby' && state) return;
      const positions = player?.pieces ?? [-1, -1, -1, -1];
      positions.forEach((step, index) => {
        const square = pieceSquare(seat, index, step);
        const key = square.join(',');
        if (!cells.has(key)) cells.set(key, { square, buttons: [] });
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'piece ' + board.colors[seat] + (player ? '' : ' empty');
        button.textContent = index + 1;
        button.dataset.piece = seat + '-' + index;
        button.setAttribute('aria-label', `${player?.name ?? board.colors[seat]}, piece ${index + 1}`);
        button.disabled = !canMove || seat !== state.you || !state.moves.includes(index);
        button.addEventListener('click', () => send('move', { piece: index }));
        cells.get(key).buttons.push(button);
      });
    });
    for (const { square, buttons } of cells.values()) {
      if (buttons.length === 1) {
        place(buttons[0], square);
        fragment.append(buttons[0]);
        continue;
      }
      const stack = document.createElement('div');
      stack.className = 'stack';
      stack.style.setProperty('--columns', Math.ceil(Math.sqrt(buttons.length)));
      place(stack, square);
      stack.append(...buttons);
      fragment.append(stack);
    }
    pieces.replaceChildren(fragment);
    if (focused) pieces.querySelector(`[data-piece="${focused}"]:enabled`)?.focus();
  }

  function renderPlayers() {
    players.replaceChildren(...state.players.map((player, seat) => {
      const row = document.createElement('li');
      row.className = 'player ' + board.colors[seat];
      row.classList.toggle('empty', !player);
      row.classList.toggle('active', state.phase === 'playing' && seat === state.turn);

      const marker = document.createElement('span');
      marker.className = 'marker';
      marker.setAttribute('aria-hidden', 'true');
      const name = document.createElement('span');
      name.className = 'player-name';
      name.textContent = player
        ? player.name + (seat === state.you ? ' (you)' : seat === 0 ? ' (host)' : '')
        : state.phase !== 'lobby' ? 'Unused seat' : seat === 0 ? 'Host seat' : 'Open seat';
      name.setAttribute('aria-label', board.colors[seat] + ' · ' + name.textContent);
      row.append(marker, name);

      if (player && state.phase !== 'lobby') {
        const score = document.createElement('span');
        score.className = 'score';
        score.textContent = player.pieces.filter(step => step === board.home).length + ' / 4';
        score.setAttribute('aria-label', score.textContent + ' pieces home');
        row.append(score);
      } else if (player && state.you === 0 && seat !== 0) {
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'quiet remove';
        remove.textContent = 'Remove';
        remove.setAttribute('aria-label', 'Remove ' + player.name);
        remove.disabled = pending || !connected;
        remove.addEventListener('click', () => send('remove', { seat }));
        row.append(remove);
      }
      return row;
    }));
  }

  function renderInvite() {
    const available = !!state.invite;
    byId('invite-message').textContent = available
      ? 'Send this command privately. Each friend runs it with Tailcat installed to open the table in their browser.'
      : state.tailcatStarting ? 'Tailcat is connecting. The invite will appear here.'
      : 'Start the game with python3 ludo.py --tailcat to generate an invite command.';
    const command = byId('invite-command');
    command.hidden = !available;
    command.value = available
      ? `tailcat forward --open-browser ${state.invite} 0:${state.port}` : '';
    byId('copy').hidden = !available;
  }

  function render() {
    const live = connected && !pending;
    if (!state) {
      renderPieces(false);
      return;
    }
    const lobby = state.phase === 'lobby';
    const playing = state.phase === 'playing';
    const yourTurn = playing && state.turn === state.you;
    const canMove = live && yourTurn && state.moves.length > 0;
    const seated = state.players.filter(Boolean).length;
    const activeName = state.players[state.turn]?.name;
    byId('turn').textContent = lobby ? 'At the table'
      : playing ? (yourTurn ? 'Your turn' : activeName + '’s turn')
      : state.turn === state.you ? 'You win!' : activeName + ' wins!';
    byId('message').textContent = lobby
      ? seated + ' of 4 seats filled. ' + (seated < 2 ? 'At least two players to start.' : 'The host can start the match.')
      : state.message + (yourTurn && state.moves.length ? ' Choose a piece.' : '');
    byId('join-form').hidden = state.you !== null || !lobby;
    const guestSeatsFull = state.players.slice(1).every(Boolean);
    byId('join-form').querySelector('button').disabled = !live || (!state.host && guestSeatsFull);
    byId('invite-button').hidden = !state.host;
    byId('leave').hidden = state.you === null || state.host || !lobby;
    byId('leave').disabled = !live;
    byId('start').hidden = state.you !== 0 || playing;
    byId('start').disabled = !live || seated < 2;
    byId('start').textContent = lobby ? 'Start match' : 'Play again';
    byId('lobby').hidden = state.you !== 0 || lobby;
    byId('lobby').disabled = !live;
    byId('dice-controls').hidden = !playing;
    byId('roll').disabled = !live || !yourTurn || state.moves.length > 0;
    byId('die').textContent = dieFaces[state.die];
    byId('die').setAttribute('aria-label', state.die
      ? `${state.players[state.rolledBy]?.name} rolled ${state.die}`
      : 'Die has not been rolled');

    const options = byId('move-options');
    options.hidden = !yourTurn || !state.moves.length;
    options.replaceChildren(...(yourTurn ? state.moves : []).map(piece => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'outline';
      button.textContent = 'Piece ' + (piece + 1);
      button.disabled = !canMove;
      button.addEventListener('click', () => send('move', { piece }));
      return button;
    }));
    renderPlayers();
    renderPieces(canMove);
    if (inviteDialog.open) renderInvite();
  }

  function showError(message = '') {
    byId('error').textContent = message;
    byId('error').hidden = !message;
  }

  async function request(action, inputs) {
    const headers = { Authorization: 'Bearer ' + token };
    const options = { headers, cache: 'no-store', signal: AbortSignal.timeout(6000) };
    if (action) {
      headers['Content-Type'] = 'application/json';
      options.method = 'POST';
      options.body = JSON.stringify({ action, version: state?.version, ...inputs });
    }
    const response = await fetch(action ? '/api/action' : '/api/state', options);
    const result = await response.json();
    if (!result.state) throw new Error('Missing table state');
    const changed = !state || result.state.version > state.version;
    if (changed) state = result.state;
    return { changed, error: result.error };
  }

  async function poll() {
    if (!pending) {
      try {
        const { changed } = await request();
        const reconnected = !connected;
        connected = true;
        if (reconnected) showError();
        if (changed || reconnected) render();
      } catch {
        connected = false;
        showError('Connection lost. Retrying…');
        render();
      }
    }
    setTimeout(poll, 900);
  }

  async function send(action, inputs = {}) {
    if (pending || !connected) return;
    pending = true;
    showError();
    render();
    try {
      const { error } = await request(action, inputs);
      showError(error);
    } catch {
      connected = false;
      showError('Connection lost. Retrying…');
    } finally {
      pending = false;
      render();
    }
  }

  byId('join-form').addEventListener('submit', event => {
    event.preventDefault();
    send('join', { name: byId('name').value });
  });
  byId('rules-button').addEventListener('click', () => rulesDialog.showModal());
  byId('start').addEventListener('click', () => send('start'));
  byId('leave').addEventListener('click', () => send('leave'));
  byId('roll').addEventListener('click', () => send('roll'));
  byId('lobby').addEventListener('click', () => {
    if (state.phase !== 'playing' || confirm('End this match and return to the lobby?')) send('lobby');
  });
  byId('invite-button').addEventListener('click', () => {
    renderInvite();
    byId('copy').textContent = 'Copy command';
    inviteDialog.showModal();
  });
  byId('copy').addEventListener('click', async () => {
    const command = byId('invite-command');
    command.select();
    try {
      await navigator.clipboard.writeText(command.value);
      byId('copy').textContent = 'Copied';
    } catch {
      byId('copy').textContent = 'Selected for copying';
    }
  });

  render();
  poll();
</script>
</body>
</html>
"""

HTML = PAGE.replace("__BOARD_LAYOUT__", json.dumps(BOARD_LAYOUT)).encode("utf-8")
TOKEN = re.compile(r"[0-9a-f]{32}")
LOCAL_HOST = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?")
TAILCAT_ADDRESS = re.compile(r"tc[A-Za-z0-9_-]+")


class TableServer(ThreadingHTTPServer):
    def __init__(self, port: int, game: Game):
        self.game = game
        super().__init__(("127.0.0.1", port), TableHandler)


class TableHandler(BaseHTTPRequestHandler):
    server: TableServer
    timeout = 6

    def log_message(self, format: str, *args: object) -> None:
        pass  # Regular state polling would otherwise fill the host's terminal.

    def reply(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self) -> bool:
        return LOCAL_HOST.fullmatch(self.headers.get("Host", "")) is not None

    def token(self) -> str:
        header = self.headers.get("Authorization", "")
        value = header.removeprefix("Bearer ")
        return value if header.startswith("Bearer ") and TOKEN.fullmatch(value) else ""

    def json_reply(self, status: HTTPStatus, payload: dict) -> None:
        self.reply(status, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        if not self.valid_host():
            self.send_error(HTTPStatus.FORBIDDEN)
        elif self.path == "/":
            self.reply(HTTPStatus.OK, HTML, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            with self.server.game.lock:
                state = self.server.game.view(self.token())
            self.json_reply(HTTPStatus.OK, {"state": state})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        if not self.valid_host() or (origin and origin != "http://" + self.headers["Host"]):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.path != "/api/action":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        token = self.token()
        if not token:
            self.send_error(HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048 or self.headers.get_content_type() != "application/json":
                raise ValueError
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, TimeoutError, RecursionError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        game = self.server.game
        status = HTTPStatus.OK
        payload = {}
        with game.lock:
            try:
                game.action(token, data)
            except GameError as error:
                status = error.status
                payload["error"] = str(error)
            payload["state"] = game.view(token)
        self.json_reply(status, payload)


def publish_tailcat(process: subprocess.Popen, game: Game) -> None:
    """Publish Tailcat's machine-readable invite while its process is running."""
    assert process.stdout is not None
    with process.stdout:
        for line in process.stdout:
            try:
                address = json.loads(line)["listenAddr"]
            except (ValueError, TypeError, KeyError):
                continue
            if not isinstance(address, str) or not TAILCAT_ADDRESS.fullmatch(address):
                continue
            with game.lock:
                game.invite = address
                game.tailcat_starting = False
                game.version += 1
            print(f"Guest command: tailcat forward --open-browser {address} 0:{game.port}", flush=True)
    process.wait()
    with game.lock:
        game.invite = ""
        game.tailcat_starting = False
        game.version += 1
    print("Tailcat stopped. The game is still running locally.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080, help="Local web port (default 8080)")
    parser.add_argument("--tailcat", action="store_true", help="Start Tailcat and generate a private invite")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    game = Game()
    try:
        server = TableServer(args.port, game)
    except OSError as error:
        parser.exit(1, f"Could not start the table: {error}\n")

    process = None
    with server:
        game.port = server.server_port
        print(f"Host link: http://127.0.0.1:{game.port}/#host={game.host_token}", flush=True)
        if args.tailcat:
            game.tailcat_starting = True
            try:
                process = subprocess.Popen(
                    ["tailcat", "serve", "--json", "--key=new", str(game.port)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                )
            except FileNotFoundError:
                parser.exit(1, "Tailcat was not found. Install it and run this command again.\n")
            except OSError as error:
                parser.exit(1, f"Could not start Tailcat: {error}\n")
            Thread(target=publish_tailcat, args=(process, game), daemon=True).start()
        else:
            print(f"Guest link on this machine: http://127.0.0.1:{game.port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
