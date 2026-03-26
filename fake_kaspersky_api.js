const express = require("express");
const app = express();

app.use(express.json());

// Root (optional - to avoid "Cannot GET /")
app.get("/", (req, res) => {
    res.send("Fake Kaspersky API is running 🚀");
});

// 🔐 Test connection
app.post("/api/test-connection", (req, res) => {
    const { username, password } = req.body;

    if (username === "admin" && password === "admin") {
        return res.json({
            status: "success",
            message: "Connected to Fake API"
        });
    }

    return res.status(401).json({
        status: "error",
        message: "Invalid credentials"
    });
});

// 🚀 Deploy
app.post("/api/deploy", (req, res) => {
    return res.json({
        status: "started",
        task_id: "TASK_" + Math.floor(Math.random() * 10000)
    });
});

// 📊 Status
app.get("/api/status/:id", (req, res) => {
    const progress = Math.floor(Math.random() * 100);

    res.json({
        task_id: req.params.id,
        progress,
        status: progress === 100 ? "completed" : "in_progress"
    });
});

// 🔥 IMPORTANT FIX (allow Docker access)
app.listen(5000, "0.0.0.0", () => {
    console.log("Fake API running on http://0.0.0.0:5000");
});