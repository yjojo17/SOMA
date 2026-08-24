// Background script: capture Instagram API response bodies via StreamFilter.
//
// Only API endpoints get a filter attached; everything else passes through
// untouched. Each captured response is decoded (content-encoding is transparent
// for JSON, so a plain TextDecoder suffices) and relayed to the tab's content
// script, which stashes it in a DOM node for the Python side to drain.
//
// Critical: every chunk is written straight back to the stream so the page loads
// exactly as it normally would. We observe, we do not modify.

// Mirrors SeleniumWireInterceptor.api_patterns.
const API_PATTERNS = [
  "/api/v1/feed/timeline",
  "/api/v1/feed/user/",
  "/api/graphql",
  "/graphql/query",
];

function isTargetUrl(url) {
  // media/<id>/info needs both fragments; the rest are plain substring matches.
  if (url.includes("/api/v1/media/")) {
    return url.includes("/info");
  }
  return API_PATTERNS.some((p) => url.includes(p));
}

browser.webRequest.onBeforeRequest.addListener(
  (details) => {
    // Skip requests with no owning tab (we relay through the tab's content script).
    if (details.tabId < 0) return {};
    if (!isTargetUrl(details.url)) return {};

    const filter = browser.webRequest.filterResponseData(details.requestId);
    const decoder = new TextDecoder("utf-8");
    let body = "";

    filter.ondata = (event) => {
      // Decode BEFORE writing back (write may consume the buffer).
      body += decoder.decode(event.data, { stream: true });
      filter.write(event.data);
    };

    filter.onstop = () => {
      body += decoder.decode(); // flush
      filter.close();
      if (body) {
        browser.tabs
          .sendMessage(details.tabId, {
            __igCapture: true,
            url: details.url,
            body: body,
          })
          .catch(() => {
            // Content script not ready (e.g. very first request before
            // document_start ran). Acceptable: the scroll loop produces many.
          });
      }
    };

    filter.onerror = () => {
      // Stream already aborted by the platform; nothing to write or close.
    };

    return {};
  },
  { urls: ["*://*.instagram.com/*"] },
  ["blocking"]
);