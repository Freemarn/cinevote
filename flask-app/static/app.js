(function () {
  var root = document.documentElement;

  // 1. Light / dark toggle, remembered in the browser
  var toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (e) {}
    });
  }

  // 2. Spinner + disable on submit, so a vote can't be double-clicked
  document.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function () {
      var btn = form.querySelector('button[type=submit]');
      if (btn) { btn.disabled = true; btn.classList.add('loading'); }
    });
  });

  // 3. Flash messages fade away after 5 seconds
  document.querySelectorAll('.flash').forEach(function (el) {
    setTimeout(function () { el.classList.add('hide'); }, 5000);
  });

  // 4. Count-up animation for admin numbers
  document.querySelectorAll('[data-count]').forEach(function (el) {
    var target = parseInt(el.getAttribute('data-count'), 10) || 0;
    var start = performance.now();
    function step(now) {
      var p = Math.min(1, (now - start) / 900);
      el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });

  // 5. Live leaderboard: ask the Flask API every 10 seconds
  var board = document.querySelector('[data-live-board]');
  if (board) {
    setInterval(function () {
      fetch('/api/leaderboard', { headers: { Accept: 'application/json' } })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
          if (!data) return;
          var max = Math.max.apply(null, [0].concat(data.results.map(function (r) { return r.votes; })));
          data.results.forEach(function (r) {
            var li = board.querySelector('[data-movie-id="' + r.id + '"]');
            if (!li) return;
            li.querySelector('.count').textContent = r.votes;
            li.querySelector('.fill').style.setProperty('--w', (max ? r.votes / max * 100 : 0) + '%');
          });
          var total = document.querySelector('[data-total]');
          if (total) total.textContent = data.total;
        })
        .catch(function () {});
    }, 10000);
  }

  // 6. Confetti after a successful vote
  if (document.querySelector('.flash-voted')) {
    for (var i = 0; i < 90; i++) {
      var c = document.createElement('span');
      c.className = 'confetti';
      c.style.left = Math.random() * 100 + 'vw';
      c.style.setProperty('--hue', Math.floor(Math.random() * 360));
      c.style.setProperty('--x', (Math.random() * 200 - 100) + 'px');
      c.style.animationDuration = (2 + Math.random() * 2) + 's';
      c.style.animationDelay = (Math.random() * 0.6) + 's';
      document.body.appendChild(c);
      setTimeout(function (el) { el.remove(); }, 5000, c);
    }
  }
})();
