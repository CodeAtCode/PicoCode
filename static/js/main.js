$(document).ready(function() {
   async function showConfirm(message) {
     return new Promise(resolve => {
       $('#confirmModalBody').text(message);
       const modalEl = document.getElementById('confirmModal');
       const modal = new bootstrap.Modal(modalEl);
       modal.show();

       $('#confirmOkBtn').off('click').on('click', () => {
         modal.hide();
         resolve(true);
       });
       modalEl.addEventListener('hidden.bs.modal', () => {
         resolve(false);
       }, { once: true });
     });
   }

function showToast(message, type = 'info', opts = {}) {
       // Use the <template> element to clone a clean toast DOM node
       const tmpl = document.getElementById('toast-template');
       if (!tmpl) {
         console.error('Toast template not found');
         return;
       }
       const clone = tmpl.content.cloneNode(true);
       const toastEl = clone.querySelector('.toast');
       if (!toastEl) {
         console.error('Toast element missing in template');
         return;
       }
       // Apply background class based on type (info, danger, etc.)
       const bgClass = `text-bg-${type}`;
       toastEl.classList.add(bgClass);
       // Set message text
       const body = toastEl.querySelector('.toast-body');
       if (body) body.textContent = message;
       // Retry button handling
       const retryBtn = toastEl.querySelector('.retry-btn');
       if (opts.showRetry && typeof opts.onRetry === 'function') {
         retryBtn.style.display = '';
         retryBtn.onclick = () => {
           const toastInstance = bootstrap.Toast.getInstance(toastEl);
           if (toastInstance) toastInstance.hide();
           opts.onRetry();
         };
       } else if (retryBtn) {
         retryBtn.style.display = 'none';
       }
       // Append to container and show
       const container = document.getElementById('toastContainer');
       if (container) container.appendChild(toastEl);
       const toast = new bootstrap.Toast(toastEl, { delay: opts.delay || 5000 });
       toast.show();
     }

   const $chatWindow = $('#chatWindow');
   const $userPrompt = $('#userPrompt');
   const $sendBtn = $('#sendBtn');
   const $clearBtn = $('#clearBtn');
   const $loadingIndicator = $('#loadingIndicator');

   marked.setOptions({
     highlight: function(code, lang) {
       if (lang && hljs.getLanguage(lang)) {
         try {
           return hljs.highlight(code, { language: lang }).value;
         } catch (err) {}
       }
       return hljs.highlightAuto(code).value;
     },
     breaks: true,
     gfm: true
   });

   let chatHistory = JSON.parse(localStorage.getItem("picocode_chat_history") || "[]");

   function renderMarkdown(text) {
     const rawHtml = marked.parse(text);
     return DOMPurify.sanitize(rawHtml);
   }

   $(document).on('click', '.toggle-section', function() {
     const sectionId = $(this).data('section');
     const $section = $('#' + sectionId);
     const $toggle = $('#' + sectionId.replace('Section', 'Toggle'));
     if ($section.css('display') === 'none') {
       $section.show();
       $toggle.text('▼');
     } else {
       $section.hide();
       $toggle.text('▶');
     }
   });

   $(document).on('click', '.delete-message-btn', async function() {
     const idx = $(this).data('index');
     if (await showConfirm('Delete this message?')) {
       chatHistory.splice(idx, 1);
       localStorage.setItem("picocode_chat_history", JSON.stringify(chatHistory));
       renderChat();
     }
   });

function renderChat() {
  if (chatHistory.length === 0) {
    $chatWindow.html('<div class="empty-state">Start a conversation by typing a message below</div>');
    return;
  }
  $chatWindow.empty();

  function createMessageElement(msg, idx) {
    const $templ = $('#chat-message-template').contents().clone();
    $templ.addClass(msg.role);
    $templ.find('.msg-author').text(msg.role === 'user' ? 'You' : 'PicoCode');
    $templ.find('.delete-message-btn').attr('data-index', idx);
    if (msg.role === 'assistant') {
      $templ.find('.msg-content').html(renderMarkdown(msg.text));
    } else {
      $templ.find('.msg-content').text(msg.text);
    }
    if (msg.context && msg.context.length > 0) {
      const $listContainer = $templ.find('.context-list');
      msg.context.forEach(c => {
        const $item = $('<div>').addClass('context-item');
        const $header = $('<div>').addClass('context-item-header');
        const $path = $('<span>').text(`📄 ${c.path} `);
        const $badge = $('<span>').addClass('badge bg-primary').text(c.score.toFixed(4));
        $header.append($path, $badge);
        $item.append($header);
        $listContainer.append($item);
      });
      $templ.find('.context-list-container').show();
    }
    $templ.find('.meta').text(msg.timestamp);
    return $templ;
  }

  chatHistory.forEach((msg, idx) => {
    const $elem = createMessageElement(msg, idx);
    $chatWindow.append($elem);
  });

  $chatWindow.scrollTop($chatWindow.prop('scrollHeight'));
  $chatWindow.find('pre code').each(function() { hljs.highlightElement(this); });
}


   function escapeHtml(text) {
     return $('<div>').text(text).html();
   }

   function addMessage(role, text, context = []) {
     const timestamp = new Date().toLocaleTimeString();
     chatHistory.push({ role, text, context, timestamp });
     localStorage.setItem("picocode_chat_history", JSON.stringify(chatHistory));
     renderChat();
   }

   async function sendMessage() {
     const prompt = $userPrompt.val().trim();
     if (!prompt) return;
     const project_id = $('#project_id').val();
     if (!project_id) {
       showToast('Please select a project or index one first.', 'warning');
       return;
     }
     const use_rag = $('#use_rag').is(':checked');
     const top_k = parseInt($('#top_k').val()) || 5;
     addMessage('user', prompt);
     $userPrompt.val('');
     $sendBtn.prop('disabled', true);
     $loadingIndicator.show();
     try {
       const response = await fetch('/code', {
         method: 'POST',
         headers: { 'Content-Type': 'application/json' },
         body: JSON.stringify({ prompt, use_rag, project_id, top_k })
       });
       const data = await response.json();
       if (data.error) {
         showToast(`Error: ${data.error}`, 'danger', { showRetry: true, onRetry: sendMessage });
       } else {
         addMessage('assistant', data.response, data.used_context || []);
       }
     } catch (err) {
       addMessage('assistant', `Error: ${err.message}`);
     } finally {
       $sendBtn.prop('disabled', false);
       $loadingIndicator.hide();
     }
   }

   $sendBtn.on('click', sendMessage);
   $userPrompt.on('keydown', function(e) {
     if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
       e.preventDefault();
       sendMessage();
     }
   });

   $clearBtn.on('click', async function() {
     if (await showConfirm('Clear chat history?')) {
       chatHistory = [];
       localStorage.removeItem('picocode_chat_history');
       renderChat();
     }
   });

   $('#createProjectBtn').on('click', async function() {
     const projectPath = $('#new_project_path').val().trim();
     const projectName = $('#new_project_name').val().trim();
     if (!projectPath) { showToast('Please enter a project path', 'warning'); return; }
     try {
       const createResponse = await fetch('/api/projects', {
         method: 'POST',
         headers: { 'Content-Type': 'application/json' },
         body: JSON.stringify({ path: projectPath, name: projectName || null })
       });
       if (!createResponse.ok) { const data = await createResponse.json(); showToast(`Failed to create project: ${data.error || 'Unknown error'}`, 'danger'); return; }
       const project = await createResponse.json();
       const projectId = project.id;
       const indexResponse = await fetch('/api/projects/index', {
         method: 'POST',
         headers: { 'Content-Type': 'application/json' },
         body: JSON.stringify({ project_id: projectId, incremental: true })
       });
       if (indexResponse.ok) {
         showToast(`Project created and indexing started!\nProject ID: ${projectId}`, 'success');
         $('#new_project_path').val('');
         $('#new_project_name').val('');
         window.location.reload();
       } else {
         const data = await indexResponse.json();
         showToast(`Project created but indexing failed: ${data.error || 'Unknown error'}`, 'danger');
         window.location.reload();
       }
     } catch (err) { showToast(`Error: ${err.message}`, 'danger'); }
   });

   $('#new_project_path').on('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); $('#createProjectBtn').click(); } });

   $(document).on('click', '.project-item', function() { $('.project-item').removeClass('active'); $(this).addClass('active'); $('#project_id').val($(this).attr('data-project-id')); });
   const $firstProject = $('.project-item').first(); if ($firstProject.length) $firstProject.addClass('active');

   setInterval(async () => {
     try {
       const response = await fetch('/projects/status');
       const projects = await response.json();
       const $list = $('#projectsList');
       $list.empty();
       if (projects.length === 0) {
         $list.append('<p class="text-muted small mb-0">No projects yet. Index a project to get started.</p>');
       } else {
projects.forEach(p => {
            const $projTemplate = $('#project-item-template').contents().clone();
            $projTemplate.attr('data-project-id', p.id);
            $projTemplate.find('.fw-bold.text-black').text(p.name || p.path.split('/').pop());
            $projTemplate.find('small.text-muted').first().text(p.path);
            const statusClass = p.status === 'ready' ? 'success' : p.status === 'indexing' ? 'warning' : 'secondary';
            $projTemplate.find('.badge').attr('class', `badge bg-${statusClass}`).attr('data-status', p.status).text(p.status);
            $projTemplate.find('.continue-index-btn').attr('data-project-id', p.id);
            $projTemplate.find('.reindex-project-btn').attr('data-project-id', p.id);
            $projTemplate.find('.delete-project-btn').attr('data-project-id', p.id);
            $list.append($projTemplate);
          });
       }
       for (const p of projects) {
         const $item = $(`[data-project-id="${p.id}"]`);
         const $badge = $item.find('.badge');
         if ($badge.length) { $badge.attr('class', `badge bg-${p.status === 'ready' ? 'success' : p.status === 'indexing' ? 'warning' : 'secondary'}`); $badge.text(p.status); }
         try {
           const detailResponse = await fetch(`/api/projects/${p.id}`);
           const details = await detailResponse.json();
           if (details.indexing_stats) {
             const $indexingInfo = $item.find('.indexing-info');
             const $fileCount = $item.find('.file-count');
             const $totalFiles = $item.find('.total-files');
             const $embeddingCount = $item.find('.embedding-count');
             const $progressBar = $item.find('.progress-bar');
             if ($indexingInfo.length && $fileCount.length && $totalFiles.length && $embeddingCount.length) {
               $fileCount.text(details.indexing_stats.file_count || 0);
               $totalFiles.text(details.indexing_stats.total_files || '0');
               $embeddingCount.text(details.indexing_stats.embedding_count || 0);
               const total = parseInt(details.indexing_stats.total_files) || 0;
               const done = parseInt(details.indexing_stats.file_count) || 0;
               const percent = total ? Math.round((done / total) * 100) : 0;
               $progressBar.css('width', percent + '%').attr('aria-valuenow', percent).text(percent + '%');
               if (details.indexing_stats.file_count > 0 || details.indexing_stats.total_files > 0) $indexingInfo.show(); else $indexingInfo.hide();
             }
           }
         } catch (detailErr) { }
       }
     } catch (err) { console.error('Error polling status:', err); }
   }, 10000);

   $(document).on('click', '.continue-index-btn', async function(e) {
     const projectId = $(this).attr('data-project-id');
     if (!await showConfirm('Continue indexing this project? This will only index new or changed files.')) return;
     try {
       const response = await fetch('/api/projects/index', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: projectId, incremental: true }) });
       if (response.ok) { const data = await response.json(); showToast(`Incremental indexing started. Status: ${data.status}`, 'info'); window.location.reload(); } else { const data = await response.json(); showToast(`Failed to start indexing: ${data.error || 'Unknown error'}`, 'danger'); }
     } catch (err) { showToast(`Error starting indexing: ${err.message}`, 'danger'); }
   });

   $(document).on('click', '.reindex-project-btn', async function(e) {
     const projectId = $(this).attr('data-project-id');
     if (!await showConfirm('Re-index this project completely? This will re-process all files.')) return;
     try {
       const response = await fetch('/api/projects/index', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: projectId, incremental: false }) });
       if (response.ok) { const data = await response.json(); showToast(`Full re-indexing started. Status: ${data.status}`, 'info'); window.location.reload(); } else { const data = await response.json(); showToast(`Failed to start re-indexing: ${data.error || 'Unknown error'}`, 'danger'); }
     } catch (err) { alert(`Error starting re-indexing: ${err.message}`); }
   });

   $(document).on('click', '.delete-project-btn', async function(e) {
     const projectId = $(this).attr('data-project-id');
     if (!await showConfirm('Delete this project and its database?')) return;
     try {
       const response = await fetch(`/projects/${projectId}`, { method: 'DELETE' });
       if (response.ok) { window.location.reload(); } else { const data = await response.json(); alert(`Failed to delete project: ${data.error || 'Unknown error'}`); }
     } catch (err) { alert(`Error deleting project: ${err.message}`); }
   });

   renderChat();
});
