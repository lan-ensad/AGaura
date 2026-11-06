# AGaura — Manual testing on a local network

How to run a mock assembly on one computer with real phones, before anything is online.

## 1. Start

```sh
uv sync
uv run python scripts/dev_seed.py          # account, a session with 5 drafts, a past session
uv run uvicorn agaura.main:app --host 0.0.0.0 --port 8000 --reload
```

`dev_seed.py` prints the account (`demo@agaura.local` / `demo-password-2026` by default), the
super-admin (`superadmin@agaura.local` / `superadmin-password-2026`) and the session IDs: a
session in preparation with 5 drafts (a resolution and one per voting method), a tribune, an
agenda and a delegate link "Paul Martin, secrétaire" (notes, chronometer) and, the first time,
a closed session "Assemblée générale 2025" whose 5 polls were voted on by 24 simulated voters
(history, minutes, exports, copy of polls). `--host 0.0.0.0` makes the server reachable from
phones on the same Wi-Fi.

Find the computer's address on the network:

```sh
hostname -I        # e.g. 192.168.1.42
```

If a firewall is active, allow the port once: `sudo ufw allow 8000/tcp` (remove the rule
afterwards with `sudo ufw delete allow 8000/tcp`).

**Open the console through that address**, not `localhost`: `http://192.168.1.42:8000/login`.
The QR codes shown in the console use the address the console was opened with, so phones can
follow them.

Emails (invitations, signup verification, password reset) are not sent in development: the
whole email, with its link, is printed in the terminal running the server within 5 seconds.

## 2. Scenarios

Tick each line on a computer (console) and two or three phones (voters).

### Super-admin and invitations
- [ ] Log in as the super-admin: it lands on "Comptes"; `/dashboard` and any `/manage/…` show
      "Non disponible pour le super-admin"; no session title appears anywhere.
- [ ] Invite an address with a name: the invitation email (terminal) holds a `/invite/…` link.
      "Renvoyer une invitation": the first link now shows "Lien non valable". Open the latest
      link in a private window, choose a password: an empty dashboard opens, address confirmed.
- [ ] Before accepting, logging in with that address fails, and "Mot de passe oublié" sends
      nothing.
- [ ] Delete the account (type its address and the super-admin's password; a wrong password
      shows "Mot de passe incorrect." and deletes nothing): it disappears with its sessions.
      With one of its sessions in session, the deletion is refused with a neutral message. The
      super-admin's own row offers no action.
- [ ] `uv run agaura user create root@example.org --superadmin`, `uv run agaura user delete
      EMAIL`, `uv run agaura stats` (invitations pending, super-admins).

### Limits against guessing
- [ ] Before the opening, posting any password to `/{id}/join` answers "La session n'a pas encore
      commencé" and never counts as a failure.
- [ ] In session, wrong passwords from one browser: after 30 refusals in 10 minutes, the
      console status bar warns the tribune ("… refusés ces 10 dernières minutes").
- [ ] 41 session IDs that do not exist from one browser (`/00010A`, `/00011A`…): the 41st and every
      session address then answer "Trop de tentatives", existing sessions included; a phone
      already in a session keeps its page.
- [ ] A generated password has 8 digits; the screen shows them as two groups of four.

### Accounts
- [ ] `/signup` explains that accounts are created by invitation (no form); the home and login
      pages offer no signup. With `AGAURA_SIGNUP=open` in `.env` (restart the server), the
      signup form comes back: create an account at `/signup`; the anti-bot check completes by
      itself; the verification email appears in the terminal; its link confirms the address.
- [ ] Log out, log in; wrong password shows an error; "Mot de passe oublié ?" sends a link
      (terminal) that sets a new password.
- [ ] `/account`: change the display name, the password, the email (link sent to the new
      address); the account log lists these events.
- [ ] The home page and `/signup` give `contact@agaura.org` to ask for an account.

### Demo
- [ ] The demo is on by default (`AGAURA_DEMO=false` in `.env` turns it off): the home page and
      `/login` offer "Essayer sans compte" with its limits and "Essayer la démo"; `/demo` lists
      its limits (5 voters per session, no export, 3 sessions and 20 polls, deletion after 2 h
      without activity, 24 h at most) and opens it once the anti-robot check completes: the
      dashboard holds "Assemblée générale de démonstration" (5 drafts: a resolution and one per
      voting method, Condorcet included) and last year's session, closed, voted on by 5
      simulated voters. The band "Démonstration" under the top bar repeats the limits with
      `contact@agaura.org`; "Se déconnecter" reads "Terminer la démo".
- [ ] `/account` of the demo offers no password, no address, no deletion: the limits and
      "Terminer la démo et tout effacer".
- [ ] Documents and the summary after the session: every export button is greyed out, with a
      note that exports need an account; typing `/manage/{id}/minutes.pdf` answers "Les
      exports demandent un compte".
- [ ] Open the session and join from 5 phones (or private windows): the sixth is refused with
      "Cette session de démonstration accepte 5 votants au plus"; the status bar and the card
      "Entrées" name the limit; saving a headcount of 40 changes nothing. Same in codes mode:
      the sixth code is refused, a code in use still moves to another phone.
- [ ] The voter page has the band "Démonstration" with "5 votants au plus ; rien n'est
      conservé…"; the projection screen says "Démonstration".
- [ ] "Terminer la démo": back on the home page with "Démo terminée"; the phone's page turns
      into the join form of an unknown session; `uv run agaura stats` shows `demo: 0`.
- [ ] `uv run agaura db backup FILE` works while a poll of the demo is open.

### Console layout
- [ ] Computer: a sidebar lists Conduite (once the session is open), Votes, Séance, Ordre du
      jour, Notes de séance, Accès et présences, Écran de projection, Documents and Réglages;
      one rubric shows at a time, the browser's back button returns to the previous one. The
      status bar stays at the top while scrolling.
- [ ] Dashboard on a small phone (or a 360 px window): one block per session (title, ID, date,
      state, then "5 brouillons" or "5 votes · 24 présents"), nothing wider than the screen.
- [ ] Phone: tabs at the bottom: the rubric the console lands on (Séance before the opening,
      Conduite after), Votes, Ordre du jour, Accès, then "Plus", which opens Notes de séance,
      Écran de projection, Documents and Réglages (Séance first once Conduite holds its tab),
      and closes once one is chosen. The end of a rubric is never hidden under the tabs; no
      horizontal scrolling.
- [ ] Before opening, there is no Conduite: the console lands on Séance, headed by "Avant
      d'ouvrir la session", one short line per step (informations, tribune, ordre du jour,
      votes préparés, mode d'accès, écran, vote d'essai; two columns on a wide screen), its mark
      ticked when ready; each step names what is missing ("Date manquante") or what it holds,
      and leads to its card, the icon and short name of its rubric at the end of the line when
      it is not Séance. "Créer" on the trial vote's line adds the draft "Tout le monde est-il
      connecté ?" (Oui, Non) with the pill "Essai" and comes back to the recap; once voted, the
      minutes and the session export leave it out. "Ouvrir la session" in the status bar lands
      on Conduite.
- [ ] Votes, before, during and after the session: every poll (drafts, "Votes en cours",
      "Votes terminés") is a tile folded on each load, its title and state pills only; a click
      on it unfolds the rest, actions included, and folds it again. A tile an action comes back
      to (publish, show on the screen) is unfolded; an unfolded "Votes en cours" tile stays so
      while a second console pushes another poll. Conduite keeps its commands unfolded.
- [ ] Each draft, once unfolded, shows one action ("Lancer le vote" in session, "Modifier"
      before) and a "⋯"
      menu that closes on a click elsewhere or Escape: "Minuterie (minutes)", "Modifier",
      "Dupliquer", "Supprimer" in session; "Dupliquer" and "Supprimer" before.
- [ ] Long help texts take one line; "En savoir plus" opens the rest (notes, codes, delegate
      links).
- [ ] In session, Conduite holds commands, each with a link to its card: the polls open now,
      the item under discussion with "Précédent" / "Suivant", the next poll with "Lancer le
      vote", a quick note, the chronometer (start with the last settings, pause, +1 min,
      reset, screen), the headcount and the QR code; right after a poll closes, "Vote clos"
      heads it with "Publier le résultat aux votants" and "Afficher sur l'écran", until either
      is done. The cards stay in their rubric (notes in Notes de séance, chronometer in Séance
      above the tribune, headcount and QR code in Accès et présences). An action started in
      Conduite comes back to Conduite; one started in a rubric comes back to that rubric.
- [ ] A second console left open redraws itself when the first one opens or closes the session.
- [ ] After closing, Conduite shows the summary and the downloads.
- [ ] With JavaScript turned off, every rubric shows one after the other and every form works.

### Session and access
- [ ] Dashboard: "Nouvelle session" (title, date, place, access "Mot de passe de la session");
      then Séance → "Modifier les informations": "Membres inscrits" and "Quorum" `1/4`.
- [ ] Console: open the session (status bar); the QR code and the 8-digit password appear in
      Conduite.
- [ ] Phone 1 scans the QR code: it joins without typing anything.
- [ ] Phone 2 opens `http://192.168.1.42:8000`, types the session ID, then the password with
      spaces or lowercase: it joins.
- [ ] The status bar shows 2 attendees without reloading, on every rubric.
- [ ] Lock entries from the status bar: a third phone (or a private window) is refused; unlock:
      it can join.
- [ ] Enter a headcount lower than the attendees: the warning appears.

### Polls (one per method)
- [ ] Votes → "Nouveau vote" → "Choix unique" (it opens on the form "Résolution"), a title,
      then "Lancer le vote": it appears on every phone within 2 seconds, without reloading.
- [ ] Vote "Pour" on phone 1, "Abstention" (the last choice, drawn like the options) on phone
      2: summary, confirmation, "Vote enregistré".
- [ ] Try to vote again on phone 1 (back button and confirm again, or reload): "Vote déjà
      enregistré.", or the poll on one line with "vote enregistré"; never a second ballot.
- [ ] The console turnout updates live; "Clore le vote"; the result shows "Adoptée" and the
      SHA-256 digest; "Publier le résultat aux votants": it appears on the phones.
- [ ] "Choix unique", form "Options libres", with 3 options; "Vote par approbation" (2 "Sièges
      à pourvoir", 4 candidates, try to tick 3); "Jugement majoritaire" (6 grades, leave one
      option ungraded = "À rejeter"). On each ballot "Vote blanc" comes last, drawn like the
      options; checking it clears the other choices, and choosing an option unchecks it.
- [ ] "Méthode Condorcet" with 4 options: on the phone, each option has a list from "Non
      classée" to "4e choix", and the counter reads "2 options sur 4 classées" after two
      ranks; give two options the same rank and leave one unranked: the summary lists them
      best first, the two at the same rank, "Non classée" dashed last with its line below.
      Checking the blank box empties every list; choosing a rank unchecks it.
- [ ] Push a poll with a 1-minute timer: the phones and the console give its closing time to the
      second; it closes by itself and disappears from the phones, and the console says "Un vote
      vient d'être clos" with the way to reload.
- [ ] Cancel a closed poll with a reason; duplicate it; push the copy.
- [ ] A phone in the middle of filling a ballot keeps its choices when another poll is pushed.
- [ ] Without JavaScript (browser setting): joining and voting still work, with manual refresh;
      an option ticked with the blank vote is refused ("Un vote blanc ne se combine avec aucun
      autre choix…"), the choices kept.

### Codes mode
- [ ] Accès et présences → "Passer aux codes individuels": phones that joined with the password
      are logged out.
- [ ] Number 12, "Télécharger le PDF des cartes": `agaura-{ID}-codes-{batch}.pdf` downloads,
      2 A4 pages (10 + 2 cards).
- [ ] Print page 1 at 100 % (no "fit to page"): crop marks line up with the card edges; a cut
      card shows a QR code of about 3 cm, the code in large type and two lines of instructions.
- [ ] "Générer et imprimer" still shows the printable page; going back after the download never
      shows the codes again (only the batch row in the console).
- [ ] Scan a card with a phone: it joins; type another code by hand: it joins.
- [ ] Enter the same code on a second phone: the first one loses access, one vote only.
- [ ] Revoke the batch: those codes stop working.

### Poll editor (on a phone)
- [ ] Console → Votes → "Nouveau vote": four cards ("Choix unique", "Vote par approbation",
      "Jugement majoritaire", "Méthode Condorcet"), the page fits the phone; choosing "Vote par
      approbation" shows only its settings, with two empty rows.
- [ ] Type options: Enter moves to the next row, "Ajouter des options" adds one, × removes one
      (never below two rows); paste a list of names (one per line) into an option: one row per
      line. "Enregistrer le brouillon".
- [ ] In "Choix unique", submit with an empty title and, under "Modifier la règle", "Autre
      seuil" at 150 %: both errors next to their fields (the fold open), everything typed still
      there; correcting a field removes its message.
- [ ] "Choix unique" opens on "Résolution": "Les votants verront :" lists Pour, Contre and
      "Abstention (vote blanc)", the labels folded under "Modifier les intitulés". "Options
      libres": two empty rows, the title kept; type two options, back to "Résolution", then
      "Options libres" again: the options typed come back. "Changer de mode" returns to the
      four cards.
- [ ] "Modifier" a draft, change the title and an option: "Brouillon enregistré.". A pushed poll
      has no "Modifier"; its /edit address returns to the console with a message.
- [ ] Reorder drafts with ↑ / ↓ (first ↑ and last ↓ disabled); duplicate; delete.
- [ ] In session: create with a duration in "Clôture automatique" and "Lancer le vote", or push
      a draft with a timer from its "⋯" menu ("Lancer avec cette minuterie"): it appears on the
      phones within 2 s and closes by itself.
- [ ] Without JavaScript: each card leads to its own settings only; "Options libres" and
      "Résolution" reload the form with the title kept; "Ajouter des options" reloads it with 5
      more rows and keeps the values; saving works.

### Copy from a past session
- [ ] In a new session, Votes → "Nouveau vote" → "Reprendre les votes d'une session passée":
      "Session à copier" lists the other sessions with polls (not the current one); copy
      "Assemblée générale 2025" with "Copier en brouillons": drafts appear after the existing
      ones, cancelled polls left out, nothing pushed.
- [ ] With a lowered quota (`uv run agaura user quota …`), the copy is refused with a message.

### Tribune and agenda
- [ ] On a phone, in a session without a tribune, console → Séance ("Plus" → Séance once the
      session is open) → "Composer la tribune": one empty row, no suggested function. Fill a
      name and a function, then "+" ("Ajouter une personne"): a new row appears and its name
      field gets the focus, with no reload.
- [ ] Leave a function without a name and save (or press Enter in a field): the form comes back
      with the message under that row and all values kept.
- [ ] Save: the list shows "Marie Dupont — Présidente de séance". "Modifier la tribune" shows the
      members' rows only.
- [ ] Empty a row and save: that person disappears. After closing the session, correcting a name
      still works.
- [ ] With JavaScript turned off, "+" reloads the console at the tribune card with one more row
      and the values kept; nothing is saved until "Enregistrer la tribune".
- [ ] Ordre du jour: paste a numbered list into the text field, then "Enregistrer l'ordre du
      jour": "Ordre du jour enregistré.", no reload; numbers are not doubled, blank lines are
      ignored. A 201-character line is refused, its message under the field, the text kept.
- [ ] Type in the field without saving: "Modifications non enregistrées" and "Annuler les
      modifications" appear; reload the console: the text typed is still there; "Annuler les
      modifications" brings back the saved agenda. Save the agenda from a second console while
      the first one holds unsaved text: that text stays, with a notice that the agenda changed.
- [ ] In session: "Suivant" and "Précédent" in Conduite; in Ordre du jour, "Point en cours" set
      to "Aucun point en cours" or to an item, then "Changer". Conduite reads "Point n sur N"
      with the item, and the projection screen follows within 2 s. A double tap on "Suivant"
      moves one item only.
- [ ] The session log shows "Tribune modifiée", "Ordre du jour modifié" and "Point en cours de
      l'ordre du jour changé", never a name.
- [ ] Session JSON: `session.tribune` and `session.agenda` are present; `recount.py` still
      prints OK.

### Attendance record
- [ ] Open the session. In "Accès et présences", "Suivi des présences" (folded under "Effectif
      compté") shows "Session ouverte" with "–" for the headcount and 0 connections.
- [ ] Two phones join. Save "Personnes comptées" = 2 from a phone (Conduite, "Effectif
      compté"): the notice appears, nothing is blocked, and "Effectif saisi" with 2 and 2 is the
      top row. Change it later (people leave): a new row, still nothing blocked.
- [ ] Push a poll, vote from a phone, then close it (or let its timer run out): rows "Vote n° 1
      lancé" and "Vote n° 1 clos" show the counts at those moments. No row appears when a phone
      joins or votes.
- [ ] Close the session: a "Session terminée" row appears. `uv run agaura audit verify` reports
      an intact chain.
- [ ] "Procès-verbal (PDF)" shows the "Suivi des présences" table after the session facts, in
      time order. The session JSON has `session.attendance`; `scripts/recount.py` still prints OK.

### Session notes
- [ ] With an item under discussion, on a phone (Plus → Notes de séance): type a note over two
      lines and tap "Ajouter". It appears at the top of the list with its time and "2. Rapport
      moral", the page does not reload, and the text area stays focused and empty.
- [ ] Type a note without sending it, then lock entries (the page reloads): the text is still
      there.
- [ ] A second console (computer) shows the note within a few seconds. Ctrl+Enter adds a note
      there, and the phone shows it.
- [ ] Tick "Note interne" and add: the note carries the "Interne" badge.
- [ ] "Modifier": change the text and the item, then save; "modifiée" appears. "Supprimer la
      note" asks first, and the note disappears on both consoles.
- [ ] Voter phone, projection screen and public results page never show a note.
- [ ] Close the session, then correct a note: the session log shows "Note modifiée après la
      clôture" without the text.
- [ ] Procès-verbal: "Déroulé de la séance" lists, item by item, the notes (not the internal
      ones) and the votes with their outcome and "Détail : vote n° n, page p". Session JSON:
      `session.notes` has no internal note, and `polls[].agenda_item` is set.

### Voter page order
- [ ] Push two polls one after the other: on the phones, the latest one is on top; published
      results come below the open polls, the latest on top. A ballot being filled keeps its
      choices when a new poll arrives above it.

### Projection screen
- [ ] Console → "Écran de projection" → "Ouvrir l'écran" on the projector (console opened
      through the network address, so the screen's QR code uses it); "Plein écran" or F11.
- [ ] Before opening: QR code and password, "Ouverture prochaine" in the header; a phone that
      scans now joins by itself within 20 s of the opening.
- [ ] Joins update attendees and quorum; a pushed poll appears within 2 s with its countdown;
      ballots update the turnout; no partial result.
- [ ] Header: "Entrées ouvertes" (in session), "Ouverture prochaine" (before opening),
      "Entrées fermées" (locked: password and QR code disappear, the address stays at the
      bottom), "Session terminée".
- [ ] Close and publish a poll: phones show the result; the screen lists it as "Vote clos", with
      no verdict, percentage or count. A cancelled poll shows "Vote annulé", without its reason,
      and offers no "Afficher sur l'écran".
- [ ] Votes › Votes terminés › "Afficher sur l'écran" (FR-75): within 1 s the screen shows the
      night card "Résultat" with the verdict, the rule and, from 10 ballots, the votes, shares
      and counts (blank ballots as "Abstention" for a resolution, "Vote blanc" otherwise);
      under 10 ballots, the verdict alone and
      "Détail des voix non affiché : moins de 10 bulletins". The poll gets "À l'écran" and
      "Retirer de l'écran", which takes it off. Showing a second result moves the first to one
      line (title and verdict) of "Questions mises aux voix"; a tie reads "Égalité : à trancher
      selon les statuts". Nothing scrolls at 1280 × 720 and 1920 × 1080.
- [ ] Agenda with 10+ items, current item 6: a window around it, "En cours" highlighted,
      "N points précédents" and "N points suivants".
- [ ] Phone console, Séance ("Plus" → Séance once the session is open), "Chronomètre et compte
      à rebours" above the tribune: one "Démarrer" only, under the line "Compte à rebours
      3 min"; the settings stay folded until "Modifier les réglages". The line follows each
      choice. Switching to "Chronomètre" hides the durations. Tapping "1 min" starts nothing.
      Label "Temps de parole", then "Démarrer": the screen shows it within 1 s and counts down.
      Pause shows "En pause" on the screen; "+1 min" works. Past zero: red "+0:05" with "Temps
      écoulé". "Réinitialiser" brings back the setup with the last mode and duration; "Autre
      durée" offers minutes and seconds. A 10-hour countdown ("10:00:00") or an hour of overtime
      fits the card with a margin, on the smallest phone and in the Conduite command
      "Chronomètre". "Chronomètre" mode counts up, titled "Chronomètre" on the screen when no
      label is typed. "Retirer de l'écran" hides it. A console open on the computer follows
      without reloading. Without JavaScript, every field is visible and the form still works.
- [ ] "Ce que la salle voit" (Écran de projection): each line that can be hidden ends with
      "Masquer" or "Afficher". Hide the agenda, then the way to join: within a second the
      screen drops the agenda, then the QR code and the password, and gives the address at its
      foot while still reading "Entrées ouvertes"; a phone with the password still joins. Show
      them again; hide and show the chronometer and a result from the same card. The session
      log names "Ordre du jour masqué à l'écran" and "Accès masqué à l'écran". Locked entries:
      the way to join has no button (the lock decides).
- [ ] With "Ce que la salle voit" in view on the computer, close a vote and show its result
      from a phone (owner logged in, or a delegate link): within a second the card follows,
      without reloading; its buttons act without reloading, the focus staying on the button.
- [ ] A delegate link with "Ce que la salle voit" only: its console shows that card alone in
      "Écran" (no screen link, no chronometer card), and its buttons act on the screen; a link
      without that right never gets the card.
- [ ] With everything at once (open poll, entries open, chronometer, agenda), nothing scrolls at
      1280×720 and 1920×1080; the way to join (QR code, password, address) takes one row beside
      the other blocks.
- [ ] Codes mode: address and card instruction, no QR code.
- [ ] "Nouveau lien d'écran": the open screen turns into "Page introuvable". Deleting a session
      does the same at once.

### Results and exports
- [ ] Close one poll of each kind: the finished poll shows "Bulletins déposés", the blank
      ballots under the ballot's own words ("Abstention" for a resolution, "Vote blanc"
      otherwise), "Suffrages exprimés", "Participation" (n / N, "x % des présents"), the rule
      applied, the result and the SHA-256 digest;
      Majority Judgment shows one stacked bar per option with the half-way line, a legend,
      "Voix par mention"; Condorcet shows the rank, duels won and lost, whether an option
      "gagne tous ses duels", and "Duels un contre un" (the matrix scrolls sideways on a phone);
      vote a cycle (e.g. 4 phones B > A > C, 3 A > C > B, 2 C > B > A): "Aucune option ne gagne
      tous ses duels" and the matrix of the strongest chains appears.
- [ ] A tie (same votes, same grades, or a perfect Condorcet cycle) reads "Égalité … : appliquez
      vos statuts", never broken.
- [ ] Documents → "Par vote" → "Bulletins (CSV)": the file opens in LibreOffice / Excel with
      accents, one column per option and one for the blank vote, one row per ballot;
      downloading it twice gives another row order.
- [ ] "Données (JSON)" of a poll and "Données complètes (JSON)" of the session;
      `uv run python scripts/recount.py <file>` prints OK for every poll (exit 0); change a
      ballot in the file: MISMATCH (exit 1).
- [ ] "Procès-verbal (PDF)": header, quorum, every pushed poll with its result and digest (same
      as the console), cancelled polls with their reason, page numbers; the tribune (names,
      functions) and the numbered agenda when filled in; no signature lines.
- [ ] Typing the CSV address of a draft or open poll shows the 409 page.
- [ ] The session log lists each download ("Bulletins exportés (CSV)", …).
- [ ] Close the session: results stay in Votes, downloads in Documents and in the Conduite
      summary.

### Public results link
- [ ] Documents → "Lien public des résultats" → "Activer le lien public", open it on a phone:
      title and date, published results and cancelled polls only (no unpublished result, no
      open poll, no draft), no horizontal scrolling; "Actualiser" shows a newly published
      result.
- [ ] "Masquer le résultat" of a published result: it stays on the public page.
- [ ] "Désactiver le lien": it shows "Page introuvable"; enabling it again gives a new address.

### Delegate links
- [ ] Console → Réglages → "Liens délégués" → "+" ("Nouveau lien délégué"): name "Claire,
      secrétariat", tick "Votes" and "Chronomètre", "Créer le lien". The page comes back to the
      new link's tile, with its address and QR code; "Copier" copies the address, which ends
      with `/delegate#k=…`.
- [ ] A phone with no account scans the QR code: "Ouverture de la console…", then the console
      without `#k=` in the address bar; the top bar shows "Accès délégué : Claire,
      secrétariat" and "Quitter"; the tabs are Conduite, Votes and Écran (the chronometer),
      no "Plus".
- [ ] The phone sees neither the session password, the QR code to join, the screen link, the
      documents nor the settings; the status bar shows the entries without "Verrouiller les
      entrées" and offers no "Terminer la session".
- [ ] From the phone, start the chronometer and push a draft: the projection screen and the
      owner's console follow within 2 s. The owner's session log shows "Vote lancé · par
      Claire, secrétariat, lien délégué".
- [ ] "Confier" in the head of the chronometer command (Conduite, in session): a panel floats
      over the command (which neither grows nor moves, the toggle staying in place), with a
      name, "Chronomètre" ticked and "Autres accès" folded; Escape or the toggle closes it.
      Create the link: its address, copy button and QR code show in the panel, without leaving
      Conduite; it is listed in Réglages; the command and the card (Séance) show "Confié à" and
      the name (a second link with the same right: "… et 1 autre", every name on hover), a link
      to the link in Réglages. Without JavaScript the page comes back to the command, open on
      the link. A phone that scans it gets Conduite and Écran, with the chronometer, whose
      "Démarrer" is the filled button. Same from the cards and commands of the headcount (Accès
      et présences), the agenda (Ordre du jour), the notes (Notes de séance), the drafts
      (Votes) and "Ce que la salle voit" (Écran de projection).
- [ ] In session, Conduite with an agenda, three drafts and one poll open shows three filled
      buttons: "Suivant", "Clore le vote" and "Lancer le vote" of the first draft only; with two
      polls open, no "Clore le vote" is filled.
- [ ] A link for "Notes de séance" alone: the phone writes notes in Conduite and in its tab
      "Notes" but cannot move the item under discussion; a link for "Ordre du jour" alone moves
      the item but sees no note.
- [ ] Owner, in Réglages: each link is a tile with its address, QR code, name and rights in
      view, nothing folded; the "+" of the card's head shows the tile of a new link (focus in
      "Nom"), "Annuler" folds it again. In the link's tile, untick "Votes" and "Enregistrer": the
      phone's console redraws itself without the Votes tab.
- [ ] Owner: "Retirer le lien": the phone turns into "Accès délégué terminé" by itself;
      "L'effacer de cet appareil" leads back to the home page.
- [ ] Before the opening, a link shows "Accès délégué" with its rights, plus the chronometer
      and the headcount when given. Closing the session ends every link ("Les liens délégués ont
      pris fin à la clôture de la session").
- [ ] A link opened with a mistyped key shows "Ce lien ne donne pas accès à la console" and
      sets no cookie.

### End
- [ ] "Quitter" on a phone ends its access; the code (codes mode) can be entered again.
- [ ] Close the session: phones show that the session is over, with the results published to
      voters below; a browser that never joined shows the message alone.
- [ ] Projection screen with two polls open, a result shown, the chronometer and the agenda:
      the three cards share one row and stay readable from the back at 1280×720.
- [ ] `uv run agaura audit verify` reports an intact chain.

## 3. Not in this version yet

Online operations (phases P0-online and the rest of P7: real email delivery, monitoring, the
restore test on the server) and the load tests and dress rehearsal (P8). Automatic backups and
`deploy/restore.sh` were tried under Docker on 2026-11-05 (PLAN, Progress); to see them locally,
run the application with `AGAURA_BACKUP_DIR=data/backups` and close a poll: a copy appears
there within a minute (`AGAURA_BACKUP_DIR=data/backups uv run agaura db backups`). HTTPS on the
local network: use `docker compose up` (Caddy, self-signed certificate) when testing a
production-like setup; `wakeLock` (screen kept awake) works only over HTTPS or on `localhost`.
