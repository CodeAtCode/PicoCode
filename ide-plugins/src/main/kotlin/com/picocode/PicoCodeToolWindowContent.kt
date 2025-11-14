package com.picocode

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.Project
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import java.awt.BorderLayout
import java.awt.Dimension
import java.awt.FlowLayout
import javax.swing.*
import java.net.HttpURLConnection
import java.net.URL
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonArray

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
    
    private fun renderChatHistory() {
        chatPanel.removeAll()
        
        for ((index, msg) in chatHistory.withIndex()) {
            val messagePanel = JPanel(BorderLayout())
            messagePanel.border = BorderFactory.createCompoundBorder(
                BorderFactory.createEmptyBorder(5, 5, 5, 5),
                BorderFactory.createLineBorder(if (msg.sender == "You") java.awt.Color.BLUE else java.awt.Color.GRAY, 1)
            )
            
            val textArea = JBTextArea(msg.message)
            textArea.isEditable = false
            textArea.lineWrap = true
            textArea.wrapStyleWord = true
            textArea.background = if (msg.sender == "You") java.awt.Color(230, 240, 255) else java.awt.Color.WHITE
            
            val headerPanel = JPanel(BorderLayout())
            headerPanel.add(JLabel("[$msg.sender]"), BorderLayout.WEST)
            
            // Add delete button for each message
            val deleteBtn = JButton("×")
            deleteBtn.preferredSize = Dimension(30, 20)
            deleteBtn.addActionListener {
                chatHistory.removeAt(index)
                renderChatHistory()
            }
            headerPanel.add(deleteBtn, BorderLayout.EAST)
            
            messagePanel.add(headerPanel, BorderLayout.NORTH)
            messagePanel.add(textArea, BorderLayout.CENTER)
            
            // Add context information if available
            if (msg.contexts.isNotEmpty()) {
                val contextText = StringBuilder("\n📎 Referenced files:\n")
                msg.contexts.forEach { ctx ->
                    contextText.append("  • ${ctx.path} (${String.format("%.3f", ctx.score)})\n")
                }
                val contextArea = JBTextArea(contextText.toString())
                contextArea.isEditable = false
                contextArea.background = java.awt.Color(250, 250, 250)
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
