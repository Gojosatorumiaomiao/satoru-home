/* Satoru Home — 轻交互 */
(function () {
  "use strict";

  // ---------- 入场动画的启用与兜底（必须最先做） ----------
  // 先取好元素、装好恢复逻辑，确认兜底就位后才加上 .js 标记。
  // 这样无论后续哪一行抛异常，正文都不会停在隐藏状态。
  var revealItems = document.querySelectorAll(".reveal");

  function revealAll() {
    for (var i = 0; i < revealItems.length; i++) {
      revealItems[i].classList.add("in");
    }
  }

  function enableRevealAnimation() {
    try {
      var reduceMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reduceMotion || !("IntersectionObserver" in window)) {
        revealAll();
        return;
      }
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        });
      }, { threshold: 0.12 });
      for (var j = 0; j < revealItems.length; j++) {
        io.observe(revealItems[j]);
      }
      // 兜底：观察器若未触发，3 秒后仍显示内容
      setTimeout(revealAll, 3000);
    } catch (err) {
      // 初始化失败也不能让正文消失
      revealAll();
    }
  }

  // 标记 + 兜底绑在一起：先装上“无论如何都恢复可见”的安全网，
  // 再启用需要隐藏才能生效的动画。
  window.addEventListener("error", revealAll);
  try {
    document.documentElement.classList.add("js");
    enableRevealAnimation();
  } catch (err) {
    document.documentElement.classList.remove("js");
    revealAll();
  }

  // 当前页导航高亮
  var here = location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll("nav a[data-nav]").forEach(function (a) {
    if (a.getAttribute("href") === here) {
      a.classList.add("active");
      a.setAttribute("aria-current", "page");
    }
  });

  // 移动端导航开关
  // 折叠只在事件确实绑定成功后才启用：先绑监听，绑定成功之后才加 .nav-ready。
  // 只要在绑定完成前抛异常，根元素就不会带 .nav-ready，链接保持默认可见可用。
  var toggle = document.querySelector(".nav-toggle");
  var links = document.getElementById("nav-links");
  try {
    if (toggle && links) {
      toggle.addEventListener("click", function () {
        var open = links.classList.toggle("open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        toggle.setAttribute("aria-label", open ? "收起导航菜单" : "展开导航菜单");
      });

      // 跳转后自动收起，避免返回时菜单残留展开
      links.addEventListener("click", function (e) {
        if (e.target.closest("a") && links.classList.contains("open")) {
          links.classList.remove("open");
          toggle.setAttribute("aria-expanded", "false");
          toggle.setAttribute("aria-label", "展开导航菜单");
        }
      });

      // 绑定成功，允许 CSS 折叠导航
      document.documentElement.classList.add("nav-ready");
    }
  } catch (err) {
    // 绑定失败：撤掉标记并复位，导航维持展开可点
    document.documentElement.classList.remove("nav-ready");
    if (links) links.classList.remove("open");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
  }

  // 画廊轻提示（不跳转、不请求外网）
  document.querySelectorAll(".art").forEach(function (el) {
    el.addEventListener("click", function () {
      var label = el.querySelector("span");
      if (!label) return;
      var old = label.textContent;
      label.textContent = "占位图 · " + old;
      setTimeout(function () { label.textContent = old; }, 1200);
    });
  });

  // 页脚年份
  var y = document.getElementById("year");
  if (y) y.textContent = new Date().getFullYear();
})();
