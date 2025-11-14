package com.picocode

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.Project
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import com.intellij.ui.JBColor
import java.awt.BorderLayout
import java.awt.Dimension
import java.awt.FlowLayout
import javax.swing.*
import javax.swing.text.html.HTMLEditorKit
import java.net.HttpURLConnection
import java.net.URL
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonArray

/**
 * Custom JEditorPane that tracks viewport width for proper HTML wrapping
 */
class WrappingEditorPane : JEditorPane() {
    override fun getScrollableTracksViewportWidth(): Boolean = true
    
    override fun getPreferredSize(): Dimension {
        // Let the parent determine the width, we only care about height
        val preferredSize = super.getPreferredSize()
        
        // If we're in a scroll pane, use the viewport width
        val parent = parent
        if (parent is JViewport) {
            val viewportWidth = parent.width
            if (viewportWidth > 0) {
                // Set a temporary size to calculate the proper height
                setSize(viewportWidth, Int.MAX_VALUE)
                preferredSize.width = viewportWidth
                preferredSize.height = super.getPreferredSize().height
            }
        }
        return preferredSize
    }
    
    override fun getMaximumSize(): Dimension {
        val maxSize = super.getMaximumSize()
        maxSize.width = Integer.MAX_VALUE
        return maxSize
    }
}

/**
 * PicoCode RAG Chat Window
 * Simple chat interface that communicates with PicoCode backend
 * Assumes PicoCode server is already running
 */
class PicoCodeToolWindowContent(private val project: Project) {
    // Chat components
    private val chatPanel = JPanel()
    private val chatScrollPane: JBScrollPane
    private val inputField = JBTextArea(3, 60)
    private val projectComboBox = JComboBox<ProjectItem>()
    
    private val gson = Gson()
    private val chatHistory = mutableListOf<ChatMessage>()
    
    data class ChatMessage(val sender: String, val message: String, val contexts: List<ContextInfo> = emptyList())
    data class ContextInfo(val path: String, val score: Float)
    data class ProjectItem(val id: String, val name: String) {
        override fun toString(): String = name
    }
    
    init {
        chatPanel.layout = BoxLayout(chatPanel, BoxLayout.Y_AXIS)
        chatScrollPane = JBScrollPane(chatPanel)
        chatScrollPane.preferredSize = Dimension(700, 500)
        inputField.lineWrap = true
        inputField.wrapStyleWord = true
        
        // Load available projects
        loadProjects()
    }
    
    private fun loadProjects() {
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                val host = getServerHost()
                val port = getServerPort()
                val url = URL("http://$host:$port/api/projects")
                val connection = url.openConnection() as HttpURLConnection
                connection.requestMethod = "GET"
                
                val response = connection.inputStream.bufferedReader().readText()
                val projects = gson.fromJson(response, JsonArray::class.java)
                
                SwingUtilities.invokeLater {
                    projectComboBox.removeAllItems()
                    projects.forEach { projectElement ->
                        val projectObj = projectElement.asJsonObject
                        val id = projectObj.get("id")?.asString ?: return@forEach
                        val name = projectObj.get("name")?.asString 
                            ?: projectObj.get("path")?.asString?.split("/")?.lastOrNull() 
                            ?: id
                        projectComboBox.addItem(ProjectItem(id, name))
                    }
                    
                    // Try to select current project
                    val currentProjectPath = project.basePath
                    if (currentProjectPath != null) {
                        for (i in 0 until projectComboBox.itemCount) {
                            val item = projectComboBox.getItemAt(i)
                            // We'll need to check against the project path - for now just select first
                            break
                        }
                    }
                }
            } catch (e: Exception) {
                // Silently fail
            }
        }
    }
    
    private fun getServerHost(): String {
        val settings = PicoCodeSettings.getInstance(project)
        return settings.state.serverHost
    }
    
    private fun getServerPort(): Int {
        val settings = PicoCodeSettings.getInstance(project)
        return settings.state.serverPort
    }
    
    fun getContent(): JComponent {
        val panel = JPanel(BorderLayout())
        
        // Top panel with project selector and re-index button
        val topPanel = JPanel(FlowLayout(FlowLayout.LEFT))
        topPanel.add(JLabel("Project:"))
        topPanel.add(projectComboBox)
        
        val refreshProjectsBtn = JButton("Refresh Projects")
        refreshProjectsBtn.addActionListener {
            loadProjects()
        }
        topPanel.add(refreshProjectsBtn)
        
        val reindexBtn = JButton("Re-index Project")
        reindexBtn.addActionListener {
            reindexProject()
        }
        topPanel.add(reindexBtn)
        
        // Chat display area
        chatScrollPane.border = BorderFactory.createTitledBorder("Chat")
        
        // Input area with buttons
        val inputPanel = JPanel(BorderLayout())
        val inputScrollPane = JBScrollPane(inputField)
        
        val buttonPanel = JPanel()
        val sendBtn = JButton("Send")
        val clearBtn = JButton("Clear History")
        
        sendBtn.addActionListener {
            sendMessage()
        }
        
        clearBtn.addActionListener {
            clearHistory()
        }
        
        // Enter key to send
        inputField.inputMap.put(KeyStroke.getKeyStroke("control ENTER"), "send")
        inputField.actionMap.put("send", object : AbstractAction() {
            override fun actionPerformed(e: java.awt.event.ActionEvent?) {
                sendMessage()
            }
        })
        
        buttonPanel.add(sendBtn)
        buttonPanel.add(clearBtn)
        
        inputPanel.add(JLabel("Your question (Ctrl+Enter to send):"), BorderLayout.NORTH)
        inputPanel.add(inputScrollPane, BorderLayout.CENTER)
        inputPanel.add(buttonPanel, BorderLayout.SOUTH)
        
        // Layout
        panel.add(topPanel, BorderLayout.NORTH)
        panel.add(chatScrollPane, BorderLayout.CENTER)
        panel.add(inputPanel, BorderLayout.SOUTH)
        
        return panel
    }
    
    /**
     * Convert markdown to HTML for rendering
     * Note: Code block backgrounds use light gray which may need adjustment for dark themes
     */
    private fun markdownToHtml(markdown: String): String {
        var html = markdown
        
        // Process markdown constructs before escaping HTML
        // Code blocks (```) - preserve content as-is
        val codeBlockPlaceholders = mutableListOf<String>()
        html = html.replace(Regex("```([\\s\\S]*?)```")) { matchResult ->
            val content = matchResult.groupValues[1]
            val placeholder = "###CODEBLOCK${codeBlockPlaceholders.size}###"
            codeBlockPlaceholders.add(content)
            placeholder
        }
        
        // Inline code (`) - preserve content
        val inlineCodePlaceholders = mutableListOf<String>()
        html = html.replace(Regex("`([^`]+)`")) { matchResult ->
            val content = matchResult.groupValues[1]
            val placeholder = "###INLINECODE${inlineCodePlaceholders.size}###"
            inlineCodePlaceholders.add(content)
            placeholder
        }
        
        // Escape HTML special characters in remaining text
        html = html
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        
        // Apply markdown formatting
        html = html
            // Bold (**text**)
            .replace(Regex("\\*\\*([^*]+)\\*\\*"), "<strong>$1</strong>")
            // Italic (*text*)
            .replace(Regex("\\*([^*]+)\\*"), "<em>$1</em>")
            // Headers
            .replace(Regex("^### (.+)$", RegexOption.MULTILINE), "<h3>$1</h3>")
            .replace(Regex("^## (.+)$", RegexOption.MULTILINE), "<h2>$1</h2>")
            .replace(Regex("^# (.+)$", RegexOption.MULTILINE), "<h1>$1</h1>")
            // Lists
            .replace(Regex("^- (.+)$", RegexOption.MULTILINE), "<li>$1</li>")
            .replace(Regex("^\\* (.+)$", RegexOption.MULTILINE), "<li>$1</li>")
        
        // Restore code blocks with proper styling and wrapping
        codeBlockPlaceholders.forEachIndexed { index, content ->
            val escapedContent = content
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            html = html.replace("###CODEBLOCK${index}###", 
                "<pre style='background-color: rgba(127, 127, 127, 0.1); padding: 8px; border-radius: 4px; border: 1px solid rgba(127, 127, 127, 0.2); white-space: pre-wrap; word-wrap: break-word; overflow-wrap: break-word;'><code>$escapedContent</code></pre>")
        }
        
        // Restore inline code with proper styling and wrapping
        inlineCodePlaceholders.forEachIndexed { index, content ->
            val escapedContent = content
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            html = html.replace("###INLINECODE${index}###", 
                "<code style='background-color: rgba(127, 127, 127, 0.15); padding: 2px 4px; border-radius: 3px; word-wrap: break-word; overflow-wrap: break-word;'>$escapedContent</code>")
        }
        
        // Wrap consecutive list items in <ul> tags
        html = html.replace(Regex("(<li>.*?</li>(?:<br/>)?)+")) { matchResult ->
            "<ul>${matchResult.value.replace("<br/>", "")}</ul>"
        }
        
        // Convert line breaks (but not inside pre/code tags)
        html = html.replace("\n", "<br/>")
        
        return "<html><body style='font-family: sans-serif; font-size: 11px; width: 100%; word-wrap: break-word; overflow-wrap: break-word;'>$html</body></html>"
    }
    
    private fun renderChatHistory() {
        chatPanel.removeAll()
        
        for ((index, msg) in chatHistory.withIndex()) {
            val messagePanel = JPanel(BorderLayout())
            
            // Ensure messagePanel respects the container width
            messagePanel.maximumSize = Dimension(Integer.MAX_VALUE, Integer.MAX_VALUE)
            
            // Use theme-aware colors
            val borderColor = if (msg.sender == "You") 
                JBColor.BLUE 
            else 
                JBColor.GRAY
            
            messagePanel.border = BorderFactory.createCompoundBorder(
                BorderFactory.createEmptyBorder(5, 5, 5, 5),
                BorderFactory.createLineBorder(borderColor, 1)
            )
            
            // Use JEditorPane for HTML/Markdown rendering with proper width tracking
            val editorPane = WrappingEditorPane()
            editorPane.contentType = "text/html"
            editorPane.editorKit = HTMLEditorKit()
            editorPane.text = markdownToHtml(msg.message)
            editorPane.isEditable = false
            editorPane.isOpaque = true
            
            // Use theme-aware background colors
            editorPane.background = if (msg.sender == "You") 
                JBColor.namedColor("EditorPane.inactiveBackground", JBColor(0xE6F0FF, 0x2D3239))
            else 
                JBColor.namedColor("EditorPane.background", JBColor.background())
            
            editorPane.putClientProperty(JEditorPane.HONOR_DISPLAY_PROPERTIES, true)
            
            val headerPanel = JPanel(BorderLayout())
            headerPanel.add(JLabel("[${msg.sender}]"), BorderLayout.WEST)
            
            // Add delete button for each message
            val deleteBtn = JButton("×")
            deleteBtn.preferredSize = Dimension(30, 20)
            deleteBtn.addActionListener {
                chatHistory.removeAt(index)
                renderChatHistory()
            }
            headerPanel.add(deleteBtn, BorderLayout.EAST)
            
            messagePanel.add(headerPanel, BorderLayout.NORTH)
            
            // Wrap editorPane in a scroll pane for long messages
            val messageScrollPane = JBScrollPane(editorPane)
            messageScrollPane.border = null
            messageScrollPane.horizontalScrollBarPolicy = ScrollPaneConstants.HORIZONTAL_SCROLLBAR_NEVER
            messageScrollPane.verticalScrollBarPolicy = ScrollPaneConstants.VERTICAL_SCROLLBAR_AS_NEEDED
            // Set maximum height to prevent messages from becoming too tall
            messageScrollPane.maximumSize = Dimension(Integer.MAX_VALUE, 300)
            messagePanel.add(messageScrollPane, BorderLayout.CENTER)
            
            // Add context information if available
            if (msg.contexts.isNotEmpty()) {
                val contextText = StringBuilder("\n📎 Referenced files:\n")
                msg.contexts.forEach { ctx ->
                    contextText.append("  • ${ctx.path} (${String.format("%.3f", ctx.score)})\n")
                }
                val contextArea = JBTextArea(contextText.toString())
                contextArea.isEditable = false
                // Use theme-aware background for context
                contextArea.background = JBColor.namedColor("Panel.background", JBColor(0xFAFAFA, 0x3C3F41))
                messagePanel.add(contextArea, BorderLayout.SOUTH)
            }
            
            chatPanel.add(messagePanel)
        }
        
        chatPanel.revalidate()
        chatPanel.repaint()
        
        // Scroll to bottom
        SwingUtilities.invokeLater {
            val verticalScrollBar = chatScrollPane.verticalScrollBar
            verticalScrollBar.value = verticalScrollBar.maximum
        }
    }
    
    /**
     * Send a message to PicoCode backend
     */
    private fun sendMessage() {
        val query = inputField.text.trim()
        if (query.isEmpty()) {
            return
        }
        
        val selectedProject = projectComboBox.selectedItem as? ProjectItem
        if (selectedProject == null) {
            SwingUtilities.invokeLater {
                JOptionPane.showMessageDialog(
                    null,
                    "Please select a project first or refresh the project list",
                    "No Project Selected",
                    JOptionPane.WARNING_MESSAGE
                )
            }
            return
        }
        
        val projectId = selectedProject.id
        val host = getServerHost()
        val port = getServerPort()
        
        // Add user message to chat
        chatHistory.add(ChatMessage("You", query))
        renderChatHistory()
        inputField.text = ""
        
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                // Send query to /code endpoint
                val queryUrl = URL("http://$host:$port/code")
                val queryConnection = queryUrl.openConnection() as HttpURLConnection
                queryConnection.requestMethod = "POST"
                queryConnection.setRequestProperty("Content-Type", "application/json")
                queryConnection.doOutput = true
                
                val queryBody = gson.toJson(mapOf(
                    "project_id" to projectId,
                    "prompt" to query,
                    "use_rag" to true,
                    "top_k" to 5
                ))
                
                queryConnection.outputStream.use { it.write(queryBody.toByteArray()) }
                
                if (queryConnection.responseCode != 200) {
                    val errorResponse = queryConnection.errorStream?.bufferedReader()?.readText() 
                        ?: "Server returned ${queryConnection.responseCode}"
                    SwingUtilities.invokeLater {
                        chatHistory.add(ChatMessage("Error", "Failed to communicate with PicoCode: $errorResponse\n" +
                            "Make sure PicoCode server is running on http://$host:$port"))
                        renderChatHistory()
                    }
                    return@executeOnPooledThread
                }
                
                val queryResponse = queryConnection.inputStream.bufferedReader().readText()
                val jsonResponse = gson.fromJson(queryResponse, JsonObject::class.java)
                
                val answer = jsonResponse.get("response")?.asString ?: "No response"
                val usedContext = jsonResponse.getAsJsonArray("used_context")
                
                val contexts = mutableListOf<ContextInfo>()
                usedContext?.forEach { ctx ->
                    val ctxObj = ctx.asJsonObject
                    val filePath = ctxObj.get("path")?.asString ?: ""
                    val score = ctxObj.get("score")?.asFloat ?: 0f
                    contexts.add(ContextInfo(filePath, score))
                }
                
                SwingUtilities.invokeLater {
                    chatHistory.add(ChatMessage("PicoCode", answer, contexts))
                    renderChatHistory()
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    chatHistory.add(ChatMessage("Error", "Failed to communicate with PicoCode: ${e.message}\n" +
                        "Make sure PicoCode server is running on http://$host:$port"))
                    renderChatHistory()
                }
            }
        }
    }
    
    /**
     * Re-index the current project
     */
    private fun reindexProject() {
        val selectedProject = projectComboBox.selectedItem as? ProjectItem
        if (selectedProject == null) {
            SwingUtilities.invokeLater {
                JOptionPane.showMessageDialog(
                    null,
                    "Please select a project first",
                    "No Project Selected",
                    JOptionPane.WARNING_MESSAGE
                )
            }
            return
        }
        
        val projectId = selectedProject.id
        val host = getServerHost()
        val port = getServerPort()
        
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                // Trigger re-indexing
                val indexUrl = URL("http://$host:$port/api/projects/index")
                val indexConnection = indexUrl.openConnection() as HttpURLConnection
                indexConnection.requestMethod = "POST"
                indexConnection.setRequestProperty("Content-Type", "application/json")
                indexConnection.doOutput = true
                
                val indexBody = gson.toJson(mapOf("project_id" to projectId))
                indexConnection.outputStream.use { it.write(indexBody.toByteArray()) }
                
                val indexResponse = indexConnection.inputStream.bufferedReader().readText()
                val indexData = gson.fromJson(indexResponse, JsonObject::class.java)
                
                SwingUtilities.invokeLater {
                    val status = indexData.get("status")?.asString ?: "unknown"
                    chatHistory.add(ChatMessage("System", "Re-indexing started. Status: $status"))
                    renderChatHistory()
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    chatHistory.add(ChatMessage("Error", "Failed to start re-indexing: ${e.message}\n" +
                        "Make sure PicoCode server is running on http://$host:$port"))
                    renderChatHistory()
                }
            }
        }
    }
    
    /**
     * Clear chat history
     */
    private fun clearHistory() {
        chatHistory.clear()
        renderChatHistory()
    }
}
