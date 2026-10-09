// Tests for the pure helpers in fastbuy.user.js (run: node tests/test_fastbuy_logic.js).
// The userscript exports its pure section when loaded under node; the browser part does not run.
"use strict";
const assert = require("assert");
const path = require("path");
const fs = require("fs");
const L = require(path.join(__dirname, "..", "fastbuy.user.js"));
const SRC = fs.readFileSync(path.join(__dirname, "..", "fastbuy.user.js"), "utf8");

let passed = 0;
const pending = [];
function test(name, fn) {
  const r = fn();
  const done = () => { passed++; console.log("ok -", name); };
  if (r && typeof r.then === "function") pending.push(r.then(done));
  else done();
}

const m = (hhmm) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3));
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

// State flow (v0.14): product (UI add 1) -> attach -> fast add 2 by GET -> attach -> bag.
const ARMED = "MJY44ZA/A";
const afterUiAdd1 = { stage: "attach", store: "R673", ts: 1, owner: "o1", armedPart: ARMED, name: "iPhone X", part: ARMED, productPath: PATH, origin: ORIGIN, atbDone: 0, atbPending: 1, atb1: "ui" };
const att = (o) => ({ qty: 2, fastCheckout: false, attachPart: "mjy44za/a", ...o });

test("1st attach landing after UI add -> 2nd add by GET with the rotated token, only once", () => {
  const r = L.onAttach(afterUiAdd1, att({ token: T2 }));
  assert.strictEqual(r.go.kind, "atb");
  assert.strictEqual(new URL(r.go.url).searchParams.get("atbtoken"), T2);
  assert.strictEqual(new URL(r.go.url).searchParams.get("product"), ARMED);
  assert.strictEqual(r.st.atbDone, 1);
  assert.strictEqual(r.st.atbPending, 2);
  assert.strictEqual(r.st.extraAtb, true);
  assert.strictEqual(r.st.stage, "attach");
  // Even if somehow back on attach with count still 1, no third GET add.
  const again = L.onAttach({ ...r.st, atbPending: 0, atbDone: 1 }, att({ token: T1 }));
  assert.strictEqual(again.go.kind, "bag");
});

test("2nd attach landing -> bag (bag check always runs; checkout/start unreachable since 1st add is UI)", () => {
  const s1 = L.onAttach(afterUiAdd1, att({ token: T2 })).st;
  for (const fastCheckout of [false, true]) {
    const r = L.onAttach(s1, att({ token: T1, fastCheckout }));
    assert.strictEqual(r.st.atbDone, 2);
    assert.strictEqual(r.go.kind, "bag");
    assert.strictEqual(r.st.stage, "bag");
  }
});

test("2nd unit GET refused unless the 1st add (same armed product) went through the UI", () => {
  // no token -> bag dropdown
  assert.strictEqual(L.onAttach(afterUiAdd1, att({ token: null })).go.kind, "bag");
  // 1st add not via UI (legacy fast state / unknown) -> no GET
  assert.strictEqual(L.onAttach({ ...afterUiAdd1, atb1: "fast" }, att({ token: T2 })).go.kind, "bag");
  assert.strictEqual(L.onAttach({ ...afterUiAdd1, atb1: undefined }, att({ token: T2 })).go.kind, "bag");
  // attach page shows another product, or none -> no GET
  assert.strictEqual(L.onAttach(afterUiAdd1, att({ token: T2, attachPart: "MJY04ZA/A" })).go.kind, "bag");
  assert.strictEqual(L.onAttach(afterUiAdd1, att({ token: T2, attachPart: null })).go.kind, "bag");
  // stored part differs from the armed one -> no GET
  assert.strictEqual(L.onAttach({ ...afterUiAdd1, part: "MJY04ZA/A" }, att({ token: T2 })).go.kind, "bag");
  // arriving with 2 already pending (2nd add landed) -> no further GET
  assert.strictEqual(L.onAttach({ ...afterUiAdd1, atbPending: 2 }, att({ token: T2 })).go.kind, "bag");
});

test("first unit never uses the direct GET (source: product-page step has no GET add)", () => {
  const step1 = SRC.slice(SRC.indexOf("// 1. Product page"), SRC.indexOf("// 2. Accessory upsell page"));
  assert.ok(step1.length > 200, "product step block found");
  assert.ok(!/atbUrl|atbToken|location\.href/.test(step1), "no GET add / navigation in the product step");
  assert.ok(/atb1: "ui"/.test(step1));
  assert.ok(/clickOwned\(atb, verifyBeforeAtb\)/.test(step1), "clicks Add to Bag");
  assert.ok(/e && !e\.disabled \? e : null/.test(step1), "waits for an enabled Add to Bag");
  // the only GET-add builder call sits in onAttach (2nd unit)
  assert.strictEqual(SRC.split("atbUrl(").length - 1, 2); // definition + onAttach
});

test("UI add 1 lands back on product page -> stop; fast add 2 lands back -> bag dropdown", () => {
  const r = L.onProductWhileAdding(afterUiAdd1);
  assert.strictEqual(r.go.kind, "stop");
  assert.strictEqual(L.onProductWhileAdding({ ...afterUiAdd1, atb1: "fast" }).go.kind, "stop");
  const s1 = L.onAttach(afterUiAdd1, att({ token: T2 })).st;
  const r2 = L.onProductWhileAdding(s1);
  assert.strictEqual(r2.go.kind, "bag");
  assert.strictEqual(r2.st.stage, "bag");
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

// ---- v0.13/v0.14 safety ----

test("FAST_CHECKOUT is off by default and @version is 0.20", () => {
  assert.match(SRC, /^const FAST_CHECKOUT = false;/m);
  assert.match(SRC, /^\/\/ @version\s+0\.20$/m);
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

test("per-tab ownership: only the tab whose window.name carries the stored owner acts", () => {
  const TTL = 5 * 60 * 1000, now = 1_000_000;
  const st = { owner: "abc-123", armedPart: "MJXV4ZA/A", ts: now - 1000, stage: "bag" };
  const mine = L.tabName("abc-123");
  assert.strictEqual(mine, "fastbuy-owner:abc-123");
  assert.strictEqual(L.tabOwner(mine), "abc-123");
  // matching window.name -> act
  assert.strictEqual(L.tabDecision(st, mine, now, TTL).kind, "act");
  // another tab: unmarked, other owner, unrelated name, bare prefix -> ignore (state is active -> grey note)
  for (const name of ["", undefined, null, L.tabName("other"), "someSiteName", "fastbuy-owner:"]) {
    const d = L.tabDecision(st, name, now, TTL);
    assert.strictEqual(d.kind, "otherTab", String(name));
    assert.strictEqual(d.active, true);
  }
  // no state -> ignore
  assert.strictEqual(L.tabDecision(null, mine, now, TTL).kind, "none");
  assert.strictEqual(L.tabDecision(null, "", now, TTL).kind, "none");
  // own flow expired -> expired (tab disarms itself); someone else's expired flow -> silent ignore
  const old = { ...st, ts: now - TTL - 1 };
  assert.strictEqual(L.tabDecision(old, mine, now, TTL).kind, "expired");
  assert.deepStrictEqual(L.tabDecision(old, "", now, TTL), { kind: "otherTab", active: false });
  // legacy state (no owner / no part) never acts, even with a fastbuy window.name
  assert.strictEqual(L.tabDecision({ ts: now - 1000 }, mine, now, TTL).kind, "legacy");
  assert.strictEqual(L.tabDecision({ owner: "abc-123", ts: now - 1000 }, mine, now, TTL).kind, "legacy");
  // two tabs read the same state: exactly one may act
  const tabs = [mine, "", L.tabName("zzz")].map((n) => L.tabDecision(st, n, now, TTL).kind);
  assert.deepStrictEqual(tabs.filter((k) => k === "act").length, 1);
});

test("per-tab ownership wiring (source check)", () => {
  // the tab is bound only after the arming read-back confirms the owner
  assert.ok(/if \(!L\.ownerMatches\(await load\(\), owner\)\) return banner[^\n]*\n[^\n]*\n\s*window\.name = L\.tabName\(owner\);/.test(SRC));
  // no unconditional adoption of the stored owner any more
  assert.ok(!/myOwner = st\.owner \|\| null/.test(SRC));
  assert.ok(/const td = L\.tabDecision\(st, window\.name, Date\.now\(\), TTL_MS\);/.test(SRC));
  assert.ok(SRC.includes('"fastbuy: 唔係呢個分頁嘅流程"'));
  // disarm and reset unbind the tab; save requires this tab's binding
  assert.ok(/const disarm = async \(\) => \{\s*try \{[^}]*GM\.deleteValue\(KEY\);\s*\} finally \{\s*clearTab\(\);/.test(SRC));
  assert.ok(/d\.kind === "reset"\) \{\s*await GM\.deleteValue\(KEY\);\s*clearTab\(\);/.test(SRC));
  assert.ok(/L\.tabOwner\(window\.name\) !== myOwner \|\| !L\.ownerMatches\(await load\(\), myOwner\)/.test(SRC));
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
  const s1 = L.onAttach(afterUiAdd1, att({ token: T2, fastCheckout: false }));
  assert.strictEqual(s1.go.kind, "atb");
  const r = L.onAttach(s1.st, att({ token: T1, fastCheckout: false }));
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
  assert.strictEqual(L.preContinueCheck({ ...ok, slotDisabled: true }).reason, "slot");
});

test("preContinueCheck: final re-check catches a day change", () => {
  const ok = { checkedStore: "R673", chosenStore: "R673", checkedDay: "9", chosenDay: "9", slotValue: "9-19:00-19:15", chosenSlot: "9-19:00-19:15" };
  assert.strictEqual(L.preContinueCheck(ok).kind, "ok");
  assert.strictEqual(L.preContinueCheck({ ...ok, checkedDay: "10" }).reason, "day"); // page switched day
  assert.strictEqual(L.preContinueCheck({ ...ok, checkedDay: undefined }).reason, "day"); // day radio unchecked
  assert.strictEqual(L.preContinueCheck({ ...ok, chosenDay: undefined }).reason, "day"); // a day radio appeared
  // page without day radios at all: both absent -> ok
  assert.strictEqual(L.preContinueCheck({ ...ok, checkedDay: undefined, chosenDay: undefined }).kind, "ok");
  // wired into the page with the checked day radio
  assert.ok(/checkedDay: \$\('input\[type=radio\]\[name\$="dayRadio"\]:checked'\)\?\.value,\s*chosenDay: checkedDay,/.test(SRC));
});

test("disabled <option>s are never slot candidates", () => {
  const opts = [
    { value: "9-19:00-19:15", disabled: true },
    { value: "9-19:30-19:45", disabled: false },
    { value: "9-10:00-10:15", disabled: false },
    { value: "", disabled: false },
    { value: "Select a time", disabled: false },
  ];
  assert.deepStrictEqual(L.slotCandidates(opts), ["9-19:30-19:45", "9-10:00-10:15"]);
  assert.strictEqual(L.chooseSlot(L.slotCandidates(opts), true, m("10:00")), "9-19:30-19:45");
  // chooseSlot itself also drops disabled option-like entries
  assert.strictEqual(L.chooseSlot(opts, true, m("10:00")), "9-19:30-19:45");
  assert.strictEqual(L.chooseSlot([{ value: "9-19:00-19:15", disabled: true }], true, m("10:00")), null);
  assert.deepStrictEqual(L.slotCandidates(null), []);
  // page uses the filtered candidates and refuses a disabled match
  assert.ok(/const allSlots = L\.slotCandidates\(slotSelect\.options\);/.test(SRC));
  assert.ok(/o\.value === choiceValue && !o\.disabled/.test(SRC));
});

test("no Continue fallback when the slot cannot be verified (source check)", () => {
  assert.ok(!/if \(!cont\) cont = enabledCont\(\)/.test(SRC));
  assert.ok(/if \(!slotSelect\) throw askSlot/.test(SRC));
});

// Item 12: slot ranking. Values "D-HH:MM-HH:MM".
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
  assert.strictEqual(L.chooseSlot(v, true, m("23:00")), null); // as today these already started -> none
});
test("chooseSlot: slots that already started are never chosen (review round 12)", () => {
  assert.strictEqual(L.chooseSlot(["9-18:00-18:15", "9-18:30-18:45"], true, m("22:30")), null);
  assert.strictEqual(L.chooseSlot(["9-18:00-18:15", "9-21:00-21:15"], true, m("22:30")), null);
  // still-future 19:00+ slot wins
  assert.strictEqual(L.chooseSlot(["9-18:00-18:15", "9-21:00-21:15"], true, m("20:30")), "9-21:00-21:15");
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
  assert.ok(/apple\\s\*pay/.test(SRC));
  // the only Apple Pay control ever clicked is the bag's checkout entry, and only on www.apple.com/.../shop/bag
  assert.ok(SRC.includes('autom === BAG_APPLE_PAY_CHECKOUT && location.hostname === "www.apple.com"'));
});

test("chooseSlot absolute time: never a past slot; 2h rule across midnight; day offset", () => {
  // now 18:00, only today 10:00 left -> already started -> null (round-12 counterexample 1)
  assert.strictEqual(L.chooseSlot(["10-10:00-10:15"], 0, 18 * 60), null);
  // now 23:30, tomorrow 00:30 and 02:00: 00:30 is only 1h away -> pick 02:00 (counterexample 2)
  assert.strictEqual(L.chooseSlot(["11-00:30-00:45", "11-02:00-02:15"], 1440, 23 * 60 + 30), "11-02:00-02:15");
  // now 23:30, tomorrow only 00:30 -> within 2h -> latest remaining = 00:30
  assert.strictEqual(L.chooseSlot(["11-00:30-00:45"], 1440, 23 * 60 + 30), "11-00:30-00:45");
  // 19:00+ still wins on a later day
  assert.strictEqual(L.chooseSlot(["11-11:00-11:15", "11-19:30-19:45"], 1440, 10 * 60), "11-19:30-19:45");
  // dayOffsetMinutes: same day 0, next day 1440, month wrap 31 -> 1
  const at = (y, mo, d, h) => Date.UTC(y, mo, d, h - 8);
  assert.strictEqual(L.dayOffsetMinutes(10, at(2026, 9, 10, 9)), 0);
  assert.strictEqual(L.dayOffsetMinutes(11, at(2026, 9, 10, 9)), 1440);
  assert.strictEqual(L.dayOffsetMinutes(1, at(2026, 9, 31, 23)), 1440);
  assert.strictEqual(L.dayOffsetMinutes(25, at(2026, 9, 10, 9)), null);
});

test("bag Apple Pay entry only on exactly /hk/shop/bag", () => {
  assert.ok(SRC.includes('location.pathname === "/hk/shop/bag"'));
  assert.ok(!/\/shop\\\/bag\$\/\.test\(location\.pathname\)/.test(SRC));
});

test("flow-advancing clicks re-check ownership", () => {
  assert.ok(SRC.includes("const clickOwned = async (el, verify)"));
  for (const v of ["guest", "pick"]) assert.ok(SRC.includes(`await clickOwned(${v})`), v);
  assert.ok(SRC.includes("await clickOwned(atb, verifyBeforeAtb)"), "atb");
  assert.ok(SRC.includes("await clickOwned(btn, verifyBeforeCheckout)"), "btn");
  assert.ok(SRC.includes("await clickOwned(cont, verifyBeforeContinue)"), "cont");
});

test("seconds count: 18:00:59 is after an 18:00 slot (review round 13)", () => {
  const at = (h, mi, se) => Date.UTC(2026, 9, 10, h - 8, mi, se);
  const now = L.hktNow(at(18, 0, 59));
  assert.strictEqual(L.chooseSlot(["10-18:00-18:15"], 0, now.minutes), null);
  assert.strictEqual(L.chooseSlot(["10-18:00-18:15", "10-18:15-18:30"], 0, L.hktNow(at(17, 59, 30)).minutes), "10-18:15-18:30" /* within 2h -> latest */);
  assert.strictEqual(L.slotStarted("10-18:00-18:15", at(17, 59, 59)), false);
  assert.strictEqual(L.slotStarted("10-18:00-18:15", at(18, 0, 0)), true);
  assert.strictEqual(L.slotStarted("11-00:30-00:45", at(23, 30, 0)), false); // tomorrow
  assert.ok(SRC.includes("if (L.slotStarted(slotChoice, Date.now()))"), "re-checked right before Continue");
});
test("Continue: slot/store/day verified synchronously after the async owner read (review round 14)", () => {
  // the verify callback runs after `await load()` and right before click, with no await in between
  assert.ok(/const assertOwned = async \(\) => \{\n\s*if \(L\.tabOwner\(window\.name\) !== myOwner \|\| !L\.ownerMatches\(await load\(\), myOwner\)\)/.test(SRC));
  for (const act of ["click\\(el\\)", "setNative\\(el, proto, value, events\\)", "location\\.href = url"])
    assert.ok(new RegExp("await assertOwned\\(\\);\\n\\s*if \\(verify\\) verify\\(\\);\\n\\s*" + act + ";").test(SRC), act);
  assert.ok(SRC.includes("await clickOwned(cont, verifyBeforeContinue)"));
  // simulate: check passes at 17:59:59.900, GM read takes 200 ms, click happens at 18:00:00.100 -> must refuse
  const at = (h, mi, se, ms) => Date.UTC(2026, 9, 10, h - 8, mi, se, ms);
  let clock = at(17, 59, 59, 900);
  const verify = () => { if (L.slotStarted("10-18:00-18:15", clock)) throw new Error("started"); };
  verify(); // fail-fast check passes
  clock += 200; // async owner read
  assert.throws(() => verify(), /started/);
});
test("Add to Bag / Check Out: DOM changes during the async owner read stop the click (review round 15)", () => {
  // Extract the real clickOwned and run it with a load() that mutates the page while it is pending.
  const ao = SRC.match(/const assertOwned = async \(\) => \{[\s\S]*?\n  \};/);
  const m = SRC.match(/const clickOwned = async \(el, verify\) => \{[\s\S]*?\n  \};/);
  assert.ok(ao && m, "assertOwned / clickOwned source");
  const run = async (mutate, verify) => {
    let clicked = 0;
    const owner = "o1";
    const fn = new Function("L", "window", "myOwner", "load", "click", ao[0] + "\nreturn " + m[0].replace(/^const clickOwned = /, "").replace(/;$/, ""));
    const clickOwned = fn(
      { tabOwner: () => owner, ownerMatches: () => true }, { name: "x" }, owner,
      async () => { mutate(); return {}; }, () => { clicked++; }
    );
    let err = null;
    try { await clickOwned({}, verify); } catch (e) { err = e; }
    return { clicked, err };
  };
  // Add to Bag: model flips from 512GB Burgundy to 1TB Black during the owner read -> no click.
  return (async () => {
    let part = "MJXV4ZA/A";
    const vAtb = () => { if (!L.productMatches("MJXV4ZA/A", part)) throw new Error("model"); };
    let r = await run(() => { part = "MJXX4ZA/A"; }, vAtb);
    assert.strictEqual(r.clicked, 0); assert.match(r.err.message, /model/);
    part = "MJXV4ZA/A";
    r = await run(() => {}, vAtb);
    assert.strictEqual(r.clicked, 1);
    // Check Out: an accessory appears in the bag during the owner read -> no click.
    const name = "iPhone 18 Pro Max 512GB Burgundy";
    let bag = [name, name];
    const vBag = () => { const d = L.bagDecision(bag, name, 2); if (d.kind !== "ok") throw new Error(d.reason); };
    r = await run(() => { bag = [name, name, "MagSafe Charger"]; }, vBag);
    assert.strictEqual(r.clicked, 0); assert.ok(r.err);
    bag = [name, name];
    r = await run(() => {}, vBag);
    assert.strictEqual(r.clicked, 1);
  })();
});
test("every flow-advancing action is owner-checked: no raw click/setNative/navigation outside the helpers (review round 16)", () => {
  const helpersStart = SRC.indexOf("  function click(el) {");
  const helpersEnd = SRC.indexOf("  const navOwned = async (url, verify) => {");
  assert.ok(helpersStart > 0 && helpersEnd > helpersStart);
  const navEnd = SRC.indexOf("};", helpersEnd) + 2;
  const outside = SRC.slice(0, helpersStart) + SRC.slice(navEnd);
  const flow = outside.slice(outside.indexOf("  try {\n    // 0. A pending add"));
  assert.ok(flow.length > 1000, "flow block found");
  assert.ok(!/location\.href\s*=/.test(flow), "raw navigation");
  assert.ok(!/(^|[^\w.])setNative\(/.test(flow), "raw setNative");
  assert.ok(!/(^|[^\w.])click\(/.test(flow), "raw click");
  assert.ok(!/\.click\(\)/.test(flow), "raw element.click()");
  assert.ok(/try \{\n\s*if \(hasGM && L\.ownerMatches\(await load\(\), myOwner\)\) await GM\.deleteValue\(KEY\);\n\s*\} finally \{\n\s*clearTab\(\);/.test(SRC), "disarm clears the tab binding even if delete fails");
});

// Run a real callback from the source against a tiny fake DOM.
const extractFn = (name) => {
  const start = SRC.indexOf(`const ${name} = () => {`);
  assert.ok(start > 0, name);
  let depth = 0, i = SRC.indexOf("{", start);
  for (; i < SRC.length; i++) { if (SRC[i] === "{") depth++; else if (SRC[i] === "}" && --depth === 0) break; }
  return SRC.slice(SRC.indexOf("() =>", start), i + 1);
};

test("verifyBeforeAtb (real source): No trade-in / No AppleCare must still be selected (review round 16)", () => {
  const body = extractFn("verifyBeforeAtb");
  const make = (dom) => new Function("L", "st", "pagePart", "name", "atb", "$", "return " + body)(
    L, { armedPart: "MJXV4ZA/A" }, () => "MJXV4ZA/A", "iPhone", dom.atb, (q) => dom[q]);
  const atb = { disabled: false };
  const base = () => ({ atb, '[data-autom="summary-productName"]': { innerText: "iPhone" }, '[data-autom="add-to-cart"]': atb,
    '[data-autom="choose-noTradeIn"]': { checked: true }, '[data-autom="noapplecare"]': { checked: true } });
  make(base())(); // unchanged -> ok
  const d1 = base(); d1['[data-autom="noapplecare"]'] = { checked: false };
  assert.throws(() => make(d1)(), /No AppleCare/);
  const d2 = base(); d2['[data-autom="choose-noTradeIn"]'] = { checked: false };
  assert.throws(() => make(d2)(), /No trade-in/);
});

test("verifyBeforeContinue (real source): selected store/day disabled or Continue changed -> stop (review round 16)", () => {
  const body = extractFn("verifyBeforeContinue");
  const at = (h, mi) => Date.UTC(2026, 9, 10, h - 8, mi, 0);
  const run = (mut) => {
    const store = { value: "R673", checked: true, disabled: false };
    const day = { value: "10", checked: true, disabled: false };
    const opt = { disabled: false };
    const sel = { value: "10-19:00-19:15", selectedOptions: [opt] };
    const cont = { disabled: false };
    let contNow = cont;
    const env = { store, day, opt, sel, setCont: (c) => { contNow = c; } };
    mut(env);
    const realNow = Date.now;
    Date.now = () => at(17, 0);
    try {
      new Function("L", "findSlotSelect", "storeRadios", "$", "chosenStore", "checkedDay", "slotChoice", "enabledCont", "cont", "return " + body)(
        L, () => sel, () => [store], (q) => (/dayRadio/.test(q) ? day : null), "R673", "10", "10-19:00-19:15", () => contNow, cont)();
    } finally { Date.now = realNow; }
  };
  run(() => {}); // unchanged -> ok
  assert.throws(() => run((e) => { e.store.disabled = true; }), /舖頭/);
  assert.throws(() => run((e) => { e.day.disabled = true; }), /日子/);
  assert.throws(() => run((e) => { e.setCont(null); }), /Continue/);
  assert.throws(() => run((e) => { e.opt.disabled = true; }), /時段/);
});

test("attach navigation re-checks ownership after the state write (review round 16)", () => {
  const step2 = SRC.slice(SRC.indexOf("// 2. Accessory upsell page"), SRC.indexOf("// 2b."));
  assert.ok(/await next\(r\.st\.stage, r\.st\);[\s\S]*await navOwned\(r\.go\.url, verifyAttach\)/.test(step2));
  assert.ok(!/location\.href\s*=/.test(step2));
});
Promise.all(pending).then(() => console.log(passed + " tests passed"));
