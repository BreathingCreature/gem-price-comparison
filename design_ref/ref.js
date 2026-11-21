(function () {
  var root = document.documentElement, saved = null;
  try { saved = localStorage.getItem("ref-theme"); } catch (e) {}
  root.setAttribute("data-theme", saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("[data-act]"); if (!t) return;
    var act = t.getAttribute("data-act");
    if (act === "theme") { var n = root.getAttribute("data-theme") === "dark" ? "light" : "dark"; root.setAttribute("data-theme", n); try { localStorage.setItem("ref-theme", n); } catch (e) {} }
    else if (act === "pipe") { var c = document.getElementById("pipe"); var o = c.getAttribute("data-open") !== "false"; c.setAttribute("data-open", o ? "false" : "true"); t.setAttribute("aria-expanded", o ? "false" : "true"); }
    else if (act === "more") { var m = document.getElementById("more-facts"); m.hidden = !m.hidden; t.textContent = m.hidden ? "Show all 14 details" : "Show fewer"; }
    else if (act === "row") { t.closest(".m-row").classList.toggle("open"); }
  });
  Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
    tab.addEventListener("click", function () {
      Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (o) { var on = o === tab; o.setAttribute("aria-selected", on ? "true" : "false"); document.getElementById(o.getAttribute("aria-controls")).hidden = !on; });
    });
  });
})();
