// Content script: receives captured response bodies from the background script
// and appends them to a hidden DOM node as NDJSON.
//
// The DOM node is the bridge to the Python side: a content script and
// driver.execute_script share the page DOM, so what we write here is readable
// via execute_script — unlike content-script JS variables, which live in an
// isolated world.

const RELAY_ID = "__ig_capture_relay";

function relayNode() {
  let n = document.getElementById(RELAY_ID);
  if (!n) {
    // A <script> with an unknown type never executes and never renders,
    // but holds textContent fine.
    n = document.createElement("script");
    n.type = "application/x-ndjson";
    n.id = RELAY_ID;
    (document.documentElement || document).appendChild(n);
  }
  return n;
}

browser.runtime.onMessage.addListener((msg) => {
  if (!msg || !msg.__igCapture) return;
  const node = relayNode();
  // JSON.stringify guarantees no literal newline in the output, so one capture
  // is exactly one NDJSON line regardless of the body's contents.
  node.textContent += JSON.stringify({ url: msg.url, body: msg.body }) + "\n";
});