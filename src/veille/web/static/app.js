// Sends the per-session CSRF token with every HTMX request (CSP forbids inline scripts).
document.addEventListener("htmx:configRequest", function (event) {
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (meta) {
    event.detail.headers["X-CSRF-Token"] = meta.getAttribute("content");
  }
});
