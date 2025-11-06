package com.picocode

import com.intellij.ide.passwordSafe.PasswordSafe
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import com.intellij.ui.components.JBTextField
import com.intellij.util.ui.FormBuilder
import org.java_websocket.client.WebSocketClient
import org.java_websocket.handshake.ServerHandshake
import java.awt.BorderLayout
import java.awt.event.ActionEvent
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.net.URI
import javax.swing.*
import java.net.HttpURLConnection
import java.net.URL
import com.google.gson.Gson
import com.google.gson.JsonObject

/**
 * Main tool window content for PicoCode RAG Assistant
 * Handles server lifecycle, API key management, and streaming responses
 */
class PicoCodeToolWindowContent(private val project: Project) {
    private val serverPort = 8000
    private val serverHost = "localhost"
    private var serverProcess: Process? = null
    private var wsClient: WebSocketClient? = null
    
    // UI Components
    private val apiKeyField = JPasswordField(20)
    private val apiBaseField = JBTextField("https://api.openai.com/v1")
    private val embeddingModelField = JBTextField("text-embedding-3-small")
    private val codingModelField = JBTextField("gpt-4")
    private val queryField = JBTextArea(3, 40)
    private val responseArea = JBTextArea(20, 40)
    private val statusLabel = JLabel("Server: Not running")
    private val progressBar = JProgressBar()
    private val retrievedFilesPanel = JPanel()
    
    private val gson = Gson()
    
    init {
        responseArea.isEditable = false
        responseArea.lineWrap = true
        retrievedFilesPanel.layout = BoxLayout(retrievedFilesPanel, BoxLayout.Y_AXIS)
        progressBar.isIndeterminate = false
        progressBar.isVisible = false
        
        // Load saved API key from IDE password safe
        loadApiKey()
    }
    
    fun getContent(): JComponent {
        val panel = JPanel(BorderLayout())
        
        // Top panel with configuration
        val configPanel = FormBuilder.createFormBuilder()
            .addLabeledComponent("API Base URL:", apiBaseField)
            .addLabeledComponent("API Key:", apiKeyField)
            .addLabeledComponent("Embedding Model:", embeddingModelField)
            .addLabeledComponent("Coding Model:", codingModelField)
            .panel
        
        // Control buttons
        val buttonPanel = JPanel()
        val startServerBtn = JButton("Start Server")
        val stopServerBtn = JButton("Stop Server")
        val indexProjectBtn = JButton("Index Project")
        val queryBtn = JButton("Query")
        val saveApiKeyBtn = JButton("Save API Key")
        
        stopServerBtn.isEnabled = false
        indexProjectBtn.isEnabled = false
        queryBtn.isEnabled = false
        
        startServerBtn.addActionListener {
            startServer()
            startServerBtn.isEnabled = false
            stopServerBtn.isEnabled = true
            indexProjectBtn.isEnabled = true
            queryBtn.isEnabled = true
        }
        
        stopServerBtn.addActionListener {
            stopServer()
            startServerBtn.isEnabled = true
            stopServerBtn.isEnabled = false
            indexProjectBtn.isEnabled = false
            queryBtn.isEnabled = false
        }
        
        saveApiKeyBtn.addActionListener {
            saveApiKey()
            JOptionPane.showMessageDialog(panel, "API Key saved securely")
        }
        
        indexProjectBtn.addActionListener {
            indexProject()
        }
        
        queryBtn.addActionListener {
            executeQuery()
        }
        
        buttonPanel.add(startServerBtn)
        buttonPanel.add(stopServerBtn)
        buttonPanel.add(indexProjectBtn)
        buttonPanel.add(queryBtn)
        buttonPanel.add(saveApiKeyBtn)
        
        // Query panel
        val queryPanel = JPanel(BorderLayout())
        queryPanel.add(JLabel("Ask a question:"), BorderLayout.NORTH)
        queryPanel.add(JBScrollPane(queryField), BorderLayout.CENTER)
        
        // Response panel with retrieved files
        val responsePanel = JPanel(BorderLayout())
        responsePanel.add(JLabel("Response:"), BorderLayout.NORTH)
        responsePanel.add(JBScrollPane(responseArea), BorderLayout.CENTER)
        
        val retrievedPanel = JPanel(BorderLayout())
        retrievedPanel.add(JLabel("Retrieved Files:"), BorderLayout.NORTH)
        retrievedPanel.add(JBScrollPane(retrievedFilesPanel), BorderLayout.CENTER)
        
        // Main content
        val mainPanel = JPanel(BorderLayout())
        mainPanel.add(configPanel, BorderLayout.NORTH)
        mainPanel.add(buttonPanel, BorderLayout.CENTER)
        
        val splitPane = JSplitPane(JSplitPane.VERTICAL_SPLIT)
        splitPane.topComponent = queryPanel
        splitPane.bottomComponent = responsePanel
        splitPane.dividerLocation = 100
        
        val splitPane2 = JSplitPane(JSplitPane.VERTICAL_SPLIT)
        splitPane2.topComponent = splitPane
        splitPane2.bottomComponent = retrievedPanel
        splitPane2.dividerLocation = 400
        
        panel.add(mainPanel, BorderLayout.NORTH)
        panel.add(splitPane2, BorderLayout.CENTER)
        panel.add(statusLabel, BorderLayout.SOUTH)
        panel.add(progressBar, BorderLayout.PAGE_END)
        
        return panel
    }
    
    /**
     * Start the Python server in the project root directory
     */
    private fun startServer() {
        val projectPath = project.basePath ?: return
        
        statusLabel.text = "Server: Starting..."
        
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                val pythonCmd = if (System.getProperty("os.name").lowercase().contains("win")) {
                    "python"
                } else {
                    "python3"
                }
                
                val processBuilder = ProcessBuilder(pythonCmd, "main.py")
                processBuilder.directory(File(projectPath))
                processBuilder.redirectErrorStream(true)
                
                serverProcess = processBuilder.start()
                
                // Read server output
                val reader = BufferedReader(InputStreamReader(serverProcess!!.inputStream))
                Thread {
                    try {
                        var line: String?
                        while (reader.readLine().also { line = it } != null) {
                            println("Server: $line")
                        }
                    } catch (e: Exception) {
                        e.printStackTrace()
                    }
                }.start()
                
                // Wait for server to start
                Thread.sleep(3000)
                
                SwingUtilities.invokeLater {
                    statusLabel.text = "Server: Running on http://$serverHost:$serverPort"
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    statusLabel.text = "Server: Failed to start - ${e.message}"
                    JOptionPane.showMessageDialog(null, "Failed to start server: ${e.message}")
                }
            }
        }
    }
    
    /**
     * Stop the Python server
     */
    private fun stopServer() {
        serverProcess?.destroy()
        serverProcess = null
        wsClient?.close()
        wsClient = null
        statusLabel.text = "Server: Stopped"
    }
    
    /**
     * Index the current project
     */
    private fun indexProject() {
        val projectPath = project.basePath ?: return
        val storeDir = File(projectPath, ".local_rag").absolutePath
        
        progressBar.isVisible = true
        progressBar.isIndeterminate = true
        statusLabel.text = "Indexing project..."
        
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                val url = URL("http://$serverHost:$serverPort/api/projects")
                val connection = url.openConnection() as HttpURLConnection
                connection.requestMethod = "POST"
                connection.setRequestProperty("Content-Type", "application/json")
                connection.doOutput = true
                
                val jsonBody = gson.toJson(mapOf(
                    "path" to projectPath,
                    "name" to project.name
                ))
                
                connection.outputStream.use { it.write(jsonBody.toByteArray()) }
                
                val responseCode = connection.responseCode
                val response = connection.inputStream.bufferedReader().readText()
                
                if (responseCode == 200) {
                    val jsonResponse = gson.fromJson(response, JsonObject::class.java)
                    val projectId = jsonResponse.get("id").asString
                    
                    // Start indexing
                    val indexUrl = URL("http://$serverHost:$serverPort/api/projects/index")
                    val indexConnection = indexUrl.openConnection() as HttpURLConnection
                    indexConnection.requestMethod = "POST"
                    indexConnection.setRequestProperty("Content-Type", "application/json")
                    indexConnection.doOutput = true
                    
                    val indexBody = gson.toJson(mapOf("project_id" to projectId))
                    indexConnection.outputStream.use { it.write(indexBody.toByteArray()) }
                    
                    val indexResponse = indexConnection.inputStream.bufferedReader().readText()
                    
                    SwingUtilities.invokeLater {
                        progressBar.isVisible = false
                        statusLabel.text = "Project indexed successfully"
                        JOptionPane.showMessageDialog(null, "Project indexed. Store: $storeDir")
                    }
                } else {
                    throw Exception("Server returned error: $responseCode - $response")
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    progressBar.isVisible = false
                    statusLabel.text = "Indexing failed: ${e.message}"
                    JOptionPane.showMessageDialog(null, "Indexing failed: ${e.message}")
                }
            }
        }
    }
    
    /**
     * Execute a query with WebSocket streaming
     */
    private fun executeQuery() {
        val query = queryField.text.trim()
        if (query.isEmpty()) {
            JOptionPane.showMessageDialog(null, "Please enter a question")
            return
        }
        
        val projectPath = project.basePath ?: return
        
        responseArea.text = ""
        retrievedFilesPanel.removeAll()
        statusLabel.text = "Querying..."
        
        // Get project ID first
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                // Get projects list to find our project
                val projectsUrl = URL("http://$serverHost:$serverPort/api/projects")
                val connection = projectsUrl.openConnection() as HttpURLConnection
                val projectsResponse = connection.inputStream.bufferedReader().readText()
                val projects = gson.fromJson(projectsResponse, Array<JsonObject>::class.java)
                
                val currentProject = projects.find { it.get("path").asString == projectPath }
                val projectId = currentProject?.get("id")?.asString ?: throw Exception("Project not found")
                
                // Use synchronous query endpoint (WebSocket would require additional dependencies)
                val queryUrl = URL("http://$serverHost:$serverPort/api/code")
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
                
                SwingUtilities.invokeLater {
                    responseArea.text = answer
                    statusLabel.text = "Query completed"
                    
                    // Display retrieved files
                    usedContext?.forEach { ctx ->
                        val ctxObj = ctx.asJsonObject
                        val filePath = ctxObj.get("path")?.asString ?: ""
                        val score = ctxObj.get("score")?.asFloat ?: 0f
                        
                        val fileButton = JButton("$filePath (score: ${String.format("%.4f", score)})")
                        fileButton.addActionListener {
                            openFileInEditor(filePath)
                        }
                        retrievedFilesPanel.add(fileButton)
                    }
                    retrievedFilesPanel.revalidate()
                    retrievedFilesPanel.repaint()
                }
            } catch (e: Exception) {
                SwingUtilities.invokeLater {
                    statusLabel.text = "Query failed: ${e.message}"
                    responseArea.text = "Error: ${e.message}"
                }
            }
        }
    }
    
    /**
     * Open a file in the editor and optionally highlight a region
     */
    private fun openFileInEditor(relativePath: String) {
        val projectPath = project.basePath ?: return
        val fullPath = File(projectPath, relativePath).absolutePath
        
        ApplicationManager.getApplication().invokeLater {
            val virtualFile = LocalFileSystem.getInstance().findFileByPath(fullPath)
            if (virtualFile != null) {
                val descriptor = OpenFileDescriptor(project, virtualFile)
                FileEditorManager.getInstance(project).openTextEditor(descriptor, true)
            } else {
                JOptionPane.showMessageDialog(null, "File not found: $fullPath")
            }
        }
    }
    
    /**
     * Load API key from IDE password safe
     */
    private fun loadApiKey() {
        try {
            val credentials = PasswordSafe.instance.get(
                com.intellij.credentialStore.CredentialAttributes(
                    "PicoCodeAPIKey",
                    "api_key"
                )
            )
            credentials?.getPasswordAsString()?.let {
                apiKeyField.text = it
            }
        } catch (e: Exception) {
            // API key not found or error, use empty
        }
    }
    
    /**
     * Save API key to IDE password safe
     */
    private fun saveApiKey() {
        val apiKey = String(apiKeyField.password)
        PasswordSafe.instance.setPassword(
            com.intellij.credentialStore.CredentialAttributes(
                "PicoCodeAPIKey",
                "api_key"
            ),
            apiKey
        )
    }
}
