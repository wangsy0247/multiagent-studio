---
name: greeting-responder
description: Respond to user greetings with a friendly and culturally-aware message. Use this skill when the user says hello, hi, or any greeting.
license: MIT
version: "1.0"
---
# Greeting Responder Skill

## Purpose
Provide warm, context-aware responses to user greetings.

## Workflow
1. **Detect the greeting type**: Identify if it's a simple "hello", a time-of-day greeting ("good morning"), or a cultural greeting.
2. **Determine the language**: Match the user's language. If the user writes in Chinese, respond in Chinese. If in English, respond in English.
3. **Add personalization**: If you know the user's name or context, include it naturally.

## Examples

### Simple greeting
User: "Hello"
Response: "Hello! How can I help you today?"

### Time-of-day greeting
User: "Good morning"
Response: "Good morning! Ready to start the day. What would you like to work on?"

### Chinese greeting
User: "你好"
Response: "你好！有什么我可以帮你的吗？"

## Anti-patterns
- Don't over-engineer the response — keep it proportional to the greeting
- Don't ask "How are you?" back unless the user asks first
- Don't use the same response every time — vary slightly
