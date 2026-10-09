// Tests for the pure helpers in fastbuy.user.js (run: node tests/test_fastbuy_logic.js).
// The userscript exports its pure section when loaded under node; the browser part does not run.
"use strict";
const assert = require("assert");
const path = require("path");
const L = require(path.join(__dirname, "..", "fastbuy.user.js"));

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log("ok -", name);
}

const T1 = "0123456789abcdef0123456789abcdef01234567"; // 40 hex
const T2 = "fedcba9876543210fedcba9876543210fedcba98";
const PATH = "/hk/shop/buy-iphone/iphone-18-pro/6.9-inch-display-2tb-burgundy";
const ORIGIN = "https://www.apple.com";

test("atbToken: 3rd part of URL-encoded as_atb", () => {
  const c = "foo=1; as_atb=" + encodeURIComponent("1.0|MjAyNi0xMC0xMFQwMDowMDowMA|" + T1) + "; bar=2";
  assert.strictEqual(L.atbToken(c), T1);
});
test("atbToken: plain (not encoded) value", () => {
  assert.strictEqual(L.atbToken("as_atb=1.0|abc|" + T1), T1);
});
test("atbToken: missing / short / non-hex / too few parts -> null", () => {
  assert.strictEqual(L.atbToken(""), null);
  assert.strictEqual(L.atbToken(undefined), null);
  assert.strictEqual(L.atbToken("x_as_atb=1|2|" + T1), null);
  assert.strictEqual(L.atbToken("as_atb=1|2|" + T1.slice(1)), null);
  assert.strictEqual(L.atbToken("as_atb=1|2|" + "g".repeat(40)), null);
  assert.strictEqual(L.atbToken("as_atb=1|" + T1), null);
  assert.strictEqual(L.atbToken("as_atb=%E0%A4%A"), null); // bad encoding
});

test("normPart / partBase", () => {
  assert.strictEqual(L.normPart("MJY44ZA/A"), "MJY44ZA/A");
  assert.strictEqual(L.normPart("mjxn4za/a"), "MJXN4ZA/A"); // attach URL uses lowercase
  assert.strictEqual(L.normPart("MJXN4ZA%2FA"), "MJXN4ZA/A");
  assert.strictEqual(L.normPart(""), null);
  assert.strictEqual(L.normPart("MJY44"), null);
  assert.strictEqual(L.partBase("MJY44ZA/A"), "MJY44");
  assert.strictEqual(L.partBase("MJXN4ZA/A"), "MJXN4");
  assert.strictEqual(L.partBase("garbage"), null);
});

test("atbUrl: params as in chrome_walkthrough R3/R4", () => {
  const u = new URL(L.atbUrl(ORIGIN, PATH, "MJY44ZA/A", T1));
  assert.strictEqual(u.origin + u.pathname, ORIGIN + PATH);
  const p = Object.fromEntries(u.searchParams);
  assert.deepStrictEqual(p, {
    product: "MJY44ZA/A",
    purchaseOption: "fullPrice",
    step: "select",
    acpart: "none",
    atbtoken: T1,
    igt: "true",
    "add-to-cart": "add-to-cart",
  });
  assert.ok(u.search.includes("product=MJY44ZA%2FA"));
});
test("atbUrl: refuses bad token / part / path", () => {
  assert.strictEqual(L.atbUrl(ORIGIN, PATH, "MJY44ZA/A", null), null);
  assert.strictEqual(L.atbUrl(ORIGIN, PATH, "MJY44ZA/A", "abc"), null);
  assert.strictEqual(L.atbUrl(ORIGIN, PATH, "nope", T1), null);
  assert.strictEqual(L.atbUrl(ORIGIN, "/hk/shop/bag", "MJY44ZA/A", T1), null);
});

test("pltn / checkoutStartUrl match R7/R8 shape", () => {
  assert.strictEqual(L.pltn("MJY44", 1), "5DAC20B5||;MJY44|"); // R7
  assert.strictEqual(L.pltn("MJXN4", 2), "5DAC20B5||;MJXN4;MJXN4|"); // R8
  assert.strictEqual(
    L.checkoutStartUrl("MJY44ZA/A", 2),
    "https://secure.store.apple.com/hk/shop/checkout/start?pltn=5DAC20B5||;MJY44;MJY44|"
  );
  assert.strictEqual(L.checkoutStartUrl("bad", 2), null);
});

// State flow: product (fast add 1) -> attach -> fast add 2 -> attach -> checkout/start.
const afterFastAdd1 = { stage: "attach", store: "R673", ts: 1, name: "iPhone X", part: "MJY44ZA/A", productPath: PATH, origin: ORIGIN, atbDone: 0, atbPending: 1, atb1: "fast", fastAtb: true };

test("1st attach landing -> 2nd add by GET with the rotated token, only once", () => {
  const r = L.onAttach(afterFastAdd1, { token: T2, qty: 2, fastCheckout: true });
  assert.strictEqual(r.go.kind, "atb");
  assert.strictEqual(new URL(r.go.url).searchParams.get("atbtoken"), T2);
  assert.strictEqual(r.st.atbDone, 1);
  assert.strictEqual(r.st.atbPending, 2);
  assert.strictEqual(r.st.extraAtb, true);
  assert.strictEqual(r.st.stage, "attach");
  // Even if somehow back on attach with count still 1, no third GET add.
  const again = L.onAttach({ ...r.st, atbPending: 0, atbDone: 1 }, { token: T1, qty: 2, fastCheckout: true });
  assert.strictEqual(again.go.kind, "bag");
});

test("2nd attach landing -> checkout/start with 2 units", () => {
  const s1 = L.onAttach(afterFastAdd1, { token: T2, qty: 2, fastCheckout: true }).st;
  const r = L.onAttach(s1, { token: T1, qty: 2, fastCheckout: true });
  assert.strictEqual(r.st.atbDone, 2);
  assert.strictEqual(r.go.kind, "checkoutStart");
  assert.strictEqual(r.st.stage, "checkoutStart");
  assert.ok(r.go.url.endsWith("pltn=5DAC20B5||;MJY44;MJY44|"));
});

test("2nd attach landing with FAST_CHECKOUT=false -> bag", () => {
  const s1 = L.onAttach(afterFastAdd1, { token: T2, qty: 2, fastCheckout: true }).st;
  const r = L.onAttach(s1, { token: T1, qty: 2, fastCheckout: false });
  assert.strictEqual(r.go.kind, "bag");
  assert.strictEqual(r.st.stage, "bag");
});

test("1st attach landing without token -> bag (dropdown fallback)", () => {
  const r = L.onAttach(afterFastAdd1, { token: null, qty: 2, fastCheckout: true });
  assert.strictEqual(r.go.kind, "bag");
  assert.strictEqual(r.st.atbDone, 1);
});

test("UI first add: 2nd add still by GET, but no checkout/start (bag check must run)", () => {
  const ui = { ...afterFastAdd1, atb1: "ui" };
  const s1 = L.onAttach(ui, { token: T2, qty: 2, fastCheckout: true });
  assert.strictEqual(s1.go.kind, "atb");
  const r = L.onAttach(s1.st, { token: T1, qty: 2, fastCheckout: true });
  assert.strictEqual(r.go.kind, "bag");
});

test("checkout/start not retried after it failed", () => {
  const s1 = L.onAttach(afterFastAdd1, { token: T2, qty: 2, fastCheckout: true }).st;
  const r = L.onAttach({ ...s1, fastCheckoutFailed: true }, { token: T1, qty: 2, fastCheckout: true });
  assert.strictEqual(r.go.kind, "bag");
});

test("fast add 1 lands back on product page -> UI path; UI failing again -> stop", () => {
  const r = L.onProductWhileAdding(afterFastAdd1);
  assert.strictEqual(r.go.kind, "ui");
  assert.strictEqual(r.st.stage, "product");
  assert.strictEqual(r.st.fastAtbFailed, true);
  const r2 = L.onProductWhileAdding({ ...afterFastAdd1, atb1: "ui" });
  assert.strictEqual(r2.go.kind, "stop");
});

test("fast add 2 lands back on product page -> bag dropdown", () => {
  const s1 = L.onAttach(afterFastAdd1, { token: T2, qty: 2, fastCheckout: true }).st;
  const r = L.onProductWhileAdding(s1);
  assert.strictEqual(r.go.kind, "bag");
  assert.strictEqual(r.st.stage, "bag");
});

test("checkout/start landing outcomes", () => {
  const st = { stage: "checkoutStart" };
  const k = (p, w) => L.onCheckoutStartLanding(st, p, w);
  assert.strictEqual(k("/hk/shop/sorry/session_expired").go.kind, "bag");
  assert.strictEqual(k("/hk/shop/sorry/session_expired").st.fastCheckoutFailed, true);
  assert.strictEqual(k("/hk/shop/checkout/start", false).go.kind, "wait");
  assert.strictEqual(k("/hk/shop/checkout/start", true).go.kind, "bag");
  assert.strictEqual(k("/hk/shop/signIn").go.kind, "continue");
  assert.strictEqual(k("/hk/shop/signIn").st.stage, "checkout");
  assert.strictEqual(k("/hk/shop/checkout").st.stage, "checkout");
  assert.strictEqual(k("/hk/shop/bag").st.stage, "bag");
  assert.strictEqual(k("/hk/shop/bag").go.kind, "continue");
  assert.strictEqual(k("/hk/shop/404").go.kind, "bag");
});

// ---- v0.13 safety ----
const fs = require("fs");
const SRC = fs.readFileSync(path.join(__dirname, "..", "fastbuy.user.js"), "utf8");

test("FAST_CHECKOUT is off by default and @version is 0.13", () => {
  assert.match(SRC, /^const FAST_CHECKOUT = false;/m);
  assert.match(SRC, /^\/\/ @version\s+0\.13$/m);
});

test("owner id: random, and guarded writes need the stored owner to match", () => {
  const a = L.newOwner(), b = L.newOwner();
  assert.ok(typeof a === "string" && a.length >= 16);
  assert.notStrictEqual(a, b);
  assert.strictEqual(L.ownerMatches({ owner: a }, a), true);
  assert.strictEqual(L.ownerMatches({ owner: b }, a), false); // another tab's state: no write/delete
  assert.strictEqual(L.ownerMatches(null, a), false); // state gone: no write
  assert.strictEqual(L.ownerMatches({}, a), false); // legacy state without owner
  assert.strictEqual(L.ownerMatches({ owner: undefined }, undefined), false);
  assert.strictEqual(L.ownerMatches({ owner: "" }, ""), false);
});

test("parseArm: store number, reset, nothing", () => {
  assert.strictEqual(L.parseArm("#fastbuy=R673"), "R673");
  assert.strictEqual(L.parseArm("#fastbuy=reset"), "reset");
  assert.strictEqual(L.parseArm("#fastbuy=resetx"), null);
  assert.strictEqual(L.parseArm("#other"), null);
  assert.strictEqual(L.parseArm(""), null);
});

test("armDecision: single active flow, reset, product required", () => {
  const TTL = 5 * 60 * 1000, now = 1_000_000;
  const active = { owner: "x", ts: now - 1000 };
  const expired = { owner: "x", ts: now - TTL - 1 };
  assert.strictEqual(L.armDecision("R673", null, now, TTL, "MJXV4ZA/A").kind, "arm");
  assert.strictEqual(L.armDecision("R673", active, now, TTL, "MJXV4ZA/A").kind, "busy"); // never overwrite
  assert.strictEqual(L.armDecision("R673", { ts: now - 1000 }, now, TTL, "MJXV4ZA/A").kind, "busy"); // ownerless but active
  assert.strictEqual(L.armDecision("R673", expired, now, TTL, "MJXV4ZA/A").kind, "arm");
  assert.strictEqual(L.armDecision("R673", null, now, TTL, null).kind, "noPart");
  assert.strictEqual(L.armDecision("reset", active, now, TTL, null).kind, "reset");
  assert.strictEqual(L.isActive({ ts: "1" }, now, TTL), false);
});

test("product binding", () => {
  assert.strictEqual(L.productMatches("MJXV4ZA/A", "MJXV4ZA%2FA"), true);
  assert.strictEqual(L.productMatches("MJXV4ZA/A", "mjxv4za/a"), true); // attach URL is lowercase
  assert.strictEqual(L.productMatches("MJXV4ZA/A", "MJY04ZA/A"), false);
  assert.strictEqual(L.productMatches("MJXV4ZA/A", null), false);
  assert.strictEqual(L.productMatches(null, "MJXV4ZA/A"), false);
  assert.strictEqual(L.productMatches(undefined, undefined), false);
});

test("bagDecision: exactly 2 target lines and nothing else", () => {
  const N = "iPhone 18 Pro Max 512GB Burgundy";
  assert.strictEqual(L.bagDecision([N, N], N, 2).kind, "ok");
  assert.strictEqual(L.bagDecision([N], N, 2).kind, "setQty");
  const more = L.bagDecision([N, N, N], N, 2);
  assert.strictEqual(more.kind, "stop");
  assert.strictEqual(more.count, 3);
  assert.strictEqual(L.bagDecision([N, N, "AirPods"], N, 2).kind, "stop");
  assert.strictEqual(L.bagDecision([N, N, "AirPods"], N, 2).reason, "others");
  assert.strictEqual(L.bagDecision([], N, 2).reason, "missing");
  assert.strictEqual(L.bagDecision([N, N], "", 2).reason, "noTarget");
  assert.strictEqual(L.bagDecision([N, N], "iPhone 18 Pro Max 1TB Burgundy", 2).kind, "stop"); // other model
});

test("mayCheckout: bag check required unless fast checkout is enabled", () => {
  assert.strictEqual(L.mayCheckout({ stage: "checkout" }, false), false);
  assert.strictEqual(L.mayCheckout({ stage: "checkout", viaFastCheckout: true }, false), false);
  assert.strictEqual(L.mayCheckout({ stage: "checkout", bagChecked: true }, false), true);
  assert.strictEqual(L.mayCheckout({ stage: "checkout", viaFastCheckout: true }, true), true);
});

test("FAST_CHECKOUT=false: attach never goes to checkout/start, always bag", () => {
  const s1 = L.onAttach(afterFastAdd1, { token: T2, qty: 2, fastCheckout: false });
  assert.strictEqual(s1.go.kind, "atb");
  const r = L.onAttach(s1.st, { token: T1, qty: 2, fastCheckout: false });
  assert.strictEqual(r.go.kind, "bag");
});

test("slotCheck: missing select / choice / unverified value -> stop", () => {
  assert.strictEqual(L.slotCheck({ selectFound: false, choice: "9-19:00-19:15", value: "9-19:00-19:15" }).kind, "stop");
  assert.strictEqual(L.slotCheck({ selectFound: true, choice: null, value: "" }).kind, "stop");
  assert.strictEqual(L.slotCheck({ selectFound: true, choice: "9-19:00-19:15", value: "9-10:00-10:15" }).kind, "stop");
  assert.strictEqual(L.slotCheck({ selectFound: true, choice: "9-19:00-19:15", value: undefined }).kind, "stop");
  assert.strictEqual(L.slotCheck({ selectFound: true, choice: "9-19:00-19:15", value: "9-19:00-19:15" }).kind, "ok");
});

test("preContinueCheck: store and slot must still be the chosen ones", () => {
  const ok = { checkedStore: "R673", chosenStore: "R673", slotValue: "9-19:00-19:15", chosenSlot: "9-19:00-19:15" };
  assert.strictEqual(L.preContinueCheck(ok).kind, "ok");
  assert.strictEqual(L.preContinueCheck({ ...ok, checkedStore: "R485" }).reason, "store");
  assert.strictEqual(L.preContinueCheck({ ...ok, checkedStore: undefined }).reason, "store");
  assert.strictEqual(L.preContinueCheck({ ...ok, slotValue: "9-10:00-10:15" }).reason, "slot");
  assert.strictEqual(L.preContinueCheck({ ...ok, chosenSlot: null, slotValue: null }).reason, "slot");
});

test("no Continue fallback when the slot cannot be verified (source check)", () => {
  assert.ok(!/if \(!cont\) cont = enabledCont\(\)/.test(SRC));
  assert.ok(/if \(!slotSelect\) throw askSlot/.test(SRC));
});

// Item 12: slot ranking. Values "D-HH:MM-HH:MM".
const m = (hhmm) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3));
test("chooseSlot: earliest 19:00+ slot wins", () => {
  const v = ["9-10:00-10:15", "9-19:30-19:45", "9-19:00-19:15", "9-20:00-20:15"];
  assert.strictEqual(L.chooseSlot(v, true, m("10:00")), "9-19:00-19:15");
});
test("chooseSlot: no 19:00+ -> earliest slot >= now+2h", () => {
  const v = ["9-11:00-11:15", "9-13:00-13:15", "9-14:30-14:45", "9-16:00-16:15"];
  assert.strictEqual(L.chooseSlot(v, true, m("11:00")), "9-13:00-13:15");
  assert.strictEqual(L.chooseSlot(v, true, m("12:01")), "9-14:30-14:45");
});
test("chooseSlot: only slots within 2h -> the latest", () => {
  const v = ["9-16:00-16:15", "9-17:30-17:45", "9-17:00-17:15"];
  assert.strictEqual(L.chooseSlot(v, true, m("16:00")), "9-17:30-17:45");
});
test("chooseSlot: later day ignores the 2h rule", () => {
  const v = ["10-10:00-10:15", "10-12:00-12:15"];
  assert.strictEqual(L.chooseSlot(v, false, m("23:00")), "10-10:00-10:15");
  assert.strictEqual(L.chooseSlot(v, true, m("23:00")), "10-12:00-12:15"); // same values as today -> latest
});
test("chooseSlot: now+2h past midnight -> nothing qualifies for rule 2 -> latest", () => {
  const v = ["9-18:00-18:15", "9-18:30-18:45"];
  assert.strictEqual(L.chooseSlot(v, true, m("22:30")), "9-18:30-18:45");
  // 19:00+ still wins over everything
  assert.strictEqual(L.chooseSlot(["9-18:00-18:15", "9-21:00-21:15"], true, m("22:30")), "9-21:00-21:15");
});
test("chooseSlot: empty / junk -> null", () => {
  assert.strictEqual(L.chooseSlot([], true, 0), null);
  assert.strictEqual(L.chooseSlot(null, true, 0), null);
  assert.strictEqual(L.chooseSlot(["", "abc"], true, 0), null);
});
test("hktNow: UTC+8 regardless of local timezone, crosses midnight", () => {
  const t = Date.UTC(2026, 9, 10, 15, 30); // 23:30 HKT on the 10th
  assert.deepStrictEqual(L.hktNow(t), { day: 10, minutes: 23 * 60 + 30 });
  const t2 = Date.UTC(2026, 9, 10, 16, 5); // 00:05 HKT on the 11th
  assert.deepStrictEqual(L.hktNow(t2), { day: 11, minutes: 5 });
});

test("unavailableText: narrow match", () => {
  assert.strictEqual(L.unavailableText("Sold Out"), true);
  assert.strictEqual(L.unavailableText("暫時缺貨"), true);
  assert.strictEqual(L.unavailableText("Add to Bag"), false);
  assert.strictEqual(L.unavailableText(undefined), false);
});

test("never-click guards still present (source check)", () => {
  assert.ok(SRC.includes('"continue-button-placeOrder", "authorizePayment", "continue-button-review"'));
  assert.ok(/place order\|authori\[sz\]e/.test(SRC));
  assert.ok(/\\bpay\\b/.test(SRC));
});

console.log(passed + " tests passed");
