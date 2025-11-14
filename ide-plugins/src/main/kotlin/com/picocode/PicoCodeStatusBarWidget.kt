package com.picocode

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.StatusBar
import com.intellij.openapi.wm.StatusBarWidget
import com.intellij.openapi.wm.StatusBarWidgetFactory
import com.intellij.util.Consumer
import com.google.gson.Gson
import com.google.gson.JsonObject
import java.awt.event.MouseEvent
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Status bar widget that displays PicoCode indexing status
 */
class PicoCodeStatusBarWidget(private val project: Project) : StatusBarWidget,
    StatusBarWidget.TextPresentation {
    
    companion object {
        const val ID = "PicoCodeStatusWidget"
        private const val POLLING_INTERVAL_SECONDS = 5L
    }
    
    private val gson = Gson()
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private var currentStatus: String = "Unknown"
    private var statusBar: StatusBar? = null
    private var projectId: String? = null
    private var indexingStats: IndexingStats? = null
    
    data class IndexingStats(
        val fileCount: Int = 0,
        val embeddingCount: Int = 0,
        val isIndexed: Boolean = false
    )
    
    init {
        // Start polling for status updates
        executor.scheduleAtFixedRate(
            { updateStatus() },
            0,
            POLLING_INTERVAL_SECONDS,
            TimeUnit.SECONDS
        )
    }
    
    override fun ID(): String = ID
    
    override fun getPresentation(): StatusBarWidget.WidgetPresentation = this
    
    override fun install(statusBar: StatusBar) {
        this.statusBar = statusBar
    }
    
    override fun dispose() {
        executor.shutdown()
        try {
            executor.awaitTermination(5, TimeUnit.SECONDS)
        } catch (e: InterruptedException) {
            executor.shutdownNow()
        }
    }
    
    override fun getText(): String {
        return when {
            currentStatus == "indexing" -> "⚡ PicoCode: Indexing..."
            currentStatus == "ready" && indexingStats?.isIndexed == true -> 
                "✓ PicoCode: ${indexingStats?.fileCount ?: 0} files"
            currentStatus == "error" -> "✗ PicoCode: Error"
            currentStatus == "created" -> "○ PicoCode: Not indexed"
            else -> "PicoCode"
        }
    }
    
    override fun getAlignment(): Float = 0.5f
    
    override fun getTooltipText(): String? {
        return when {
            currentStatus == "indexing" -> "PicoCode is indexing your project..."
            currentStatus == "ready" && indexingStats != null -> 
                "PicoCode: ${indexingStats?.fileCount} files, ${indexingStats?.embeddingCount} embeddings indexed"
            currentStatus == "error" -> "PicoCode indexing error occurred"
            currentStatus == "created" -> "PicoCode: Project created but not indexed yet"
            else -> "PicoCode status unknown - check if server is running"
        }
    }
    
    override fun getClickConsumer(): Consumer<MouseEvent>? {
        return Consumer { 
            // Open the PicoCode chat dialog on click
            ApplicationManager.getApplication().invokeLater {
                val chatContent = PicoCodeToolWindowContent(project)
                val dialog = object : com.intellij.openapi.ui.DialogWrapper(project) {
                    init {
                        init()
                        title = "PicoCode RAG Assistant"
                    }
                    
                    override fun createCenterPanel(): javax.swing.JComponent {
                        return chatContent.getContent()
                    }
                    
                    override fun createActions(): Array<com.intellij.openapi.ui.DialogWrapper.DialogWrapperAction> {
                        return emptyArray() // No default OK/Cancel buttons
                    }
                }
                dialog.show()
            }
        }
    }
    
    private fun updateStatus() {
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                val projectPath = project.basePath ?: return@executeOnPooledThread
                
                // Get or create project to get project ID
                if (projectId == null) {
                    projectId = getOrCreateProject(projectPath)
                }
                
                projectId?.let { id ->
                    // Fetch project status
                    val status = fetchProjectStatus(id)
                    currentStatus = status.first
                    indexingStats = status.second
                    
                    // Update status bar on EDT
                    ApplicationManager.getApplication().invokeLater {
                        statusBar?.updateWidget(ID)
                    }
                }
            } catch (e: Exception) {
                // Silently fail - don't spam logs if server is not running
                currentStatus = "Unavailable"
            }
        }
    }
    
    private fun getOrCreateProject(projectPath: String): String? {
        return try {
            val settings = PicoCodeSettings.getInstance(project)
            val host = settings.state.serverHost
            val port = settings.state.serverPort
            
            val url = URL("http://$host:$port/api/projects")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.doOutput = true
            
            val body = gson.toJson(mapOf(
                "path" to projectPath,
                "name" to project.name
            ))
            connection.outputStream.use { it.write(body.toByteArray()) }
            
            val response = connection.inputStream.bufferedReader().readText()
            val json = gson.fromJson(response, JsonObject::class.java)
            json.get("id")?.asString
        } catch (e: Exception) {
            null
        }
    }
    
    private fun fetchProjectStatus(projectId: String): Pair<String, IndexingStats?> {
        return try {
            val settings = PicoCodeSettings.getInstance(project)
            val host = settings.state.serverHost
            val port = settings.state.serverPort
            
            val url = URL("http://$host:$port/api/projects/$projectId")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            
            val response = connection.inputStream.bufferedReader().readText()
            val json = gson.fromJson(response, JsonObject::class.java)
            
            val status = json.get("status")?.asString ?: "unknown"
            val statsJson = json.getAsJsonObject("indexing_stats")
            
            val stats = if (statsJson != null) {
                IndexingStats(
                    fileCount = statsJson.get("file_count")?.asInt ?: 0,
                    embeddingCount = statsJson.get("embedding_count")?.asInt ?: 0,
                    isIndexed = statsJson.get("is_indexed")?.asBoolean ?: false
                )
            } else {
                null
            }
            
            Pair(status, stats)
        } catch (e: Exception) {
            Pair("unavailable", null)
        }
    }
}

/**
 * Factory for creating the status bar widget
 */
class PicoCodeStatusBarWidgetFactory : StatusBarWidgetFactory {
    override fun getId(): String = PicoCodeStatusBarWidget.ID
    
    override fun getDisplayName(): String = "PicoCode Status"
    
    override fun isAvailable(project: Project): Boolean = true
    
    override fun createWidget(project: Project): StatusBarWidget {
        return PicoCodeStatusBarWidget(project)
    }
    
    override fun disposeWidget(widget: StatusBarWidget) {
        // Disposal is handled by the widget itself
    }
    
    override fun canBeEnabledOn(statusBar: StatusBar): Boolean = true
}
