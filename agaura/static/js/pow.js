// Anti-robot proof of work (ALTCHA protocol), served by the application (strict CSP).
//
// Fetches a challenge, finds the number whose SHA-256 with the salt equals it, stores the
// solution in the hidden "altcha" field and enables the submit button. Status texts come from
// the data- attributes of the widget: this file holds no visible text.
//
// Pages served over plain HTTP on a local network have no crypto.subtle: a small pure
// JavaScript SHA-256 takes over there.
(function () {
  "use strict";

  const widget = document.querySelector("[data-pow]");
  if (!widget) {
    return;
  }
  const form = widget.closest("form");
  const field = widget.querySelector('input[name="altcha"]');
  const status = widget.querySelector("[data-pow-status]");
  const submit = form ? form.querySelector('[type="submit"]') : null;
  const refreshSeconds = Number(widget.dataset.powRefresh || "0");
  const subtle = window.crypto && window.crypto.subtle && window.isSecureContext
    ? window.crypto.subtle
    : null;
  const encoder = new TextEncoder();
  const BATCH = 500;

  function show(key) {
    if (status && widget.dataset[key]) {
      status.textContent = widget.dataset[key];
    }
    widget.dataset.state = key;
  }

  // --- Pure JavaScript SHA-256 (FIPS 180-4), for short ASCII inputs -----------------------

  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const W = new Uint32Array(64);

  function rotr(x, n) {
    return (x >>> n) | (x << (32 - n));
  }

  function sha256Hex(text) {
    const bytes = encoder.encode(text);
    const length = bytes.length;
    const blocks = (length + 9 + 63) >> 6;
    const words = new Uint32Array(blocks * 16);
    for (let i = 0; i < length; i++) {
      words[i >> 2] |= bytes[i] << (24 - (i & 3) * 8);
    }
    words[length >> 2] |= 0x80 << (24 - (length & 3) * 8);
    words[blocks * 16 - 1] = length * 8;
    const H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    for (let block = 0; block < blocks; block++) {
      for (let t = 0; t < 16; t++) {
        W[t] = words[block * 16 + t];
      }
      for (let t = 16; t < 64; t++) {
        const x = W[t - 15];
        const y = W[t - 2];
        const s0 = rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3);
        const s1 = rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10);
        W[t] = (W[t - 16] + s0 + W[t - 7] + s1) | 0;
      }
      let a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
      for (let t = 0; t < 64; t++) {
        const t1 = (h + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[t] + W[t]) | 0;
        const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        h = g;
        g = f;
        f = e;
        e = (d + t1) | 0;
        d = c;
        c = b;
        b = a;
        a = (t1 + t2) | 0;
      }
      H[0] = (H[0] + a) | 0;
      H[1] = (H[1] + b) | 0;
      H[2] = (H[2] + c) | 0;
      H[3] = (H[3] + d) | 0;
      H[4] = (H[4] + e) | 0;
      H[5] = (H[5] + f) | 0;
      H[6] = (H[6] + g) | 0;
      H[7] = (H[7] + h) | 0;
    }
    let hex = "";
    for (let i = 0; i < 8; i++) {
      hex += (H[i] >>> 0).toString(16).padStart(8, "0");
    }
    return hex;
  }

  function toHex(buffer) {
    const bytes = new Uint8Array(buffer);
    let hex = "";
    for (let i = 0; i < bytes.length; i++) {
      hex += bytes[i].toString(16).padStart(2, "0");
    }
    return hex;
  }

  // --- Solving --------------------------------------------------------------------------

  function pause() {
    return new Promise((resolve) => setTimeout(resolve, 0));
  }

  async function solve(challenge) {
    const max = Number(challenge.maxnumber);
    for (let start = 0; start <= max; start += BATCH) {
      const end = Math.min(start + BATCH - 1, max);
      if (subtle) {
        const digests = [];
        for (let n = start; n <= end; n++) {
          digests.push(subtle.digest("SHA-256", encoder.encode(challenge.salt + n)));
        }
        const results = await Promise.all(digests);
        for (let i = 0; i < results.length; i++) {
          if (toHex(results[i]) === challenge.challenge) {
            return start + i;
          }
        }
      } else {
        for (let n = start; n <= end; n++) {
          if (sha256Hex(challenge.salt + n) === challenge.challenge) {
            return n;
          }
        }
        await pause(); // keep the page responsive
      }
    }
    return null;
  }

  let timer = null;

  async function run() {
    if (submit) {
      submit.disabled = true;
    }
    field.value = "";
    show("powWorking");
    try {
      const response = await fetch(widget.dataset.challengeUrl, {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error("challenge " + response.status);
      }
      const challenge = await response.json();
      const number = await solve(challenge);
      if (number === null) {
        throw new Error("no solution");
      }
      field.value = btoa(JSON.stringify({
        algorithm: challenge.algorithm,
        challenge: challenge.challenge,
        number: number,
        salt: challenge.salt,
        signature: challenge.signature,
      }));
      show("powDone");
      if (submit) {
        submit.disabled = false;
      }
      if (refreshSeconds > 0) {
        clearTimeout(timer);
        timer = setTimeout(run, refreshSeconds * 1000); // before the challenge expires
      }
    } catch (error) {
      show("powFailed");
    }
  }

  run();
})();
