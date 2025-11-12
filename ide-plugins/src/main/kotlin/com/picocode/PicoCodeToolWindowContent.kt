package com.picocode

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.Project
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import java.awt.BorderLayout
import javax.swing.*
import java.net.HttpURLConnection
import java.net.URL
import com.google.gson.Gson
import com.google.gson.JsonObject

/**
 * PicoCode RAG Chat Window
 * Simple chat interface that communicates with PicoCode backend
 * Assumes PicoCode server is already running
 */
class PicoCodeToolWindowContent(private val project: Project) {
    // Chat components
    private val chatArea = JBTextArea(25, 60)
    private val inputField = JBTextArea(3, 60)
    
    private val gson = Gson()
    private val chatHistory = mutableListOf<Pair<String, String>>() // (query, response)
    
    init {
        chatArea.isEditable = false
        chatArea.lineWrap = true
        chatArea.wrapStyleWord = true
        inputField.lineWrap = true
        inputField.wrapStyleWord = true
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
        
        // Add a re-index button at the top
        val reindexBtn = JButton("Re-index Project")
        reindexBtn.addActionListener {
            reindexProject()
        }
        val topPanel = JPanel(BorderLayout())
        topPanel.add(reindexBtn, BorderLayout.EAST)
        
        // Chat display area
        val chatScrollPane = JBScrollPane(chatArea)
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
     * Send a message to PicoCode backend
     */
    private fun sendMessage() {
        val query = inputField.text.trim()
        if (query.isEmpty()) {
            return
        }
        
        val projectPath = project.basePath ?: return
        val host = getServerHost()
        val port = getServerPort()
        
        // Add user message to chat
        appendToChat("You", query)
        inputField.text = ""
        
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                // Get or create project
                val projectsUrl = URL("http://$host:$port/api/projects")
                val createConnection = projectsUrl.openConnection() as HttpURLConnection
                createConnection.requestMethod = "POST"
                createConnection.setRequestProperty("Content-Type", "application/json")
                createConnection.doOutput = true
                
                val createBody = gson.toJson(mapOf(
                    "path" to projectPath,
                    "name" to project.name
                ))
                createConnection.outputStream.use { it.write(createBody.toByteArray()) }
                
                val createResponse = createConnection.inputStream.bufferedReader().readText()
                val projectData = gson.fromJson(createResponse, JsonObject::class.java)
                val projectId = projectData.get("id").asString
                
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
                
                val queryResponse = queryConnection.inputStream.bufferedReader().readText()
                val jsonResponse = gson.fromJson(queryResponse, JsonObject::class.java)
                
                val answer = jsonResponse.get("response")?.asString ?: "No response"
                val usedContext = jsonResponse.getAsJsonArray("used_context")
                
                // Add response to chat
                SwingUtilities.invokeLater {
                    appendToChat("PicoCode", answer)
                    
                    // Add file references if any
                    usedContext?.let { contexts ->
                        if (contexts.size() > 0) {
                            val fileRefs = StringBuilder("\n📎 Referenced files:\n")
                            contexts.forEach { ctx ->
                                val ctxObj = ctx.asJsonObject
                                val filePath = ctxObj.get("path")?.asString ?: ""
                                val score = ctxObj.get("score")?.asFloat ?: 0f
                                fileRefs.append("  • $filePath (${String.format("%.3f", score)})\n")
                            }
                            chatArea.append(fileRefs.toString())
                        }
                    }
                    
                    chatHistory.add(Pair(query, answer))
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    appendToChat("Error", "Failed to communicate with PicoCode: ${e.message}\n" +
                        "Make sure PicoCode server is running on http://$host:$port")
                }
            }
        }
    }
    
    /**
     * Re-index the current project
     */
    private fun reindexProject() {
        val projectPath = project.basePath ?: return
        val host = getServerHost()
        val port = getServerPort()
        
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                // Get or create project to get project ID
                val projectsUrl = URL("http://$host:$port/api/projects")
                val createConnection = projectsUrl.openConnection() as HttpURLConnection
                createConnection.requestMethod = "POST"
                createConnection.setRequestProperty("Content-Type", "application/json")
                createConnection.doOutput = true
                
                val createBody = gson.toJson(mapOf(
                    "path" to projectPath,
                    "name" to project.name
                ))
                createConnection.outputStream.use { it.write(createBody.toByteArray()) }
                
                val createResponse = createConnection.inputStream.bufferedReader().readText()
                val projectData = gson.fromJson(createResponse, JsonObject::class.java)
                val projectId = projectData.get("id").asString
                
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
                    appendToChat("System", "Re-indexing started. Status: $status")
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    appendToChat("Error", "Failed to start re-indexing: ${e.message}\n" +
                        "Make sure PicoCode server is running on http://$host:$port")
                }
            }
        }
    }
    
    /**
     * Clear chat history
     */
    private fun clearHistory() {
        chatHistory.clear()
        chatArea.text = ""
    }
    
    /**
     * Append a message to the chat area
     */
    private fun appendToChat(sender: String, message: String) {
        SwingUtilities.invokeLater {
            if (chatArea.text.isNotEmpty()) {
                chatArea.append("\n\n")
            }
            chatArea.append("[$sender]\n$message")
            chatArea.caretPosition = chatArea.document.length
        }
    }
}
