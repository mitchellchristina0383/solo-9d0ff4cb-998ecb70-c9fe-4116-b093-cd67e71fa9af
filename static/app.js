// 渐进增强：没有 JS 时所有表单仍可完整完成演练。
document.addEventListener("click", function (event) {
  var staleBtn = event.target.closest("button[value='stale']");
  if (!staleBtn) return;
  var form = staleBtn.closest("form");
  if (!form) return;
  var input = form.querySelector("input[name='boot_no']");
  if (input && !input.disabled) {
    var current = parseInt(input.value, 10);
    if (!Number.isNaN(current)) {
      input.value = String(Math.max(0, current - 1));
    }
  }
});
