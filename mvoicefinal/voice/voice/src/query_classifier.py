# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# -------------------------------------------------------------------------
"""
Query Classifier - Determines whether queries need agent processing.

This module provides classification logic to route user queries:
- SIMPLE: Handled directly by VoiceLive (greetings, chitchat, simple questions)
- DATA_LOOKUP: Requires LangGraph agent for tool calls (database queries, etc.)
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from voice.src.set_logging import logger


class QueryType(str, Enum):
    """Classification of user queries for routing decisions."""
    SIMPLE = "simple"           # VoiceLive handles directly
    DATA_LOOKUP = "data_lookup" # Needs agent for tool calls
    CONVERSATIONAL = "conversational"  # Chitchat, greetings


@dataclass
class ClassificationResult:
    """Result of query classification."""
    query_type: QueryType
    confidence: float  # 0.0 to 1.0
    matched_keywords: list[str] = field(default_factory=list)
    reason: Optional[str] = None


@dataclass 
class ClassifierConfig:
    """Configuration for the query classifier."""
    
    # Keywords that indicate data lookup is needed
    # Customize these for your domain
    data_keywords: list[str] = field(default_factory=lambda: [
        # Customer data domain
        "customer", "machine", "machines", "address", "addresses",
        "serial", "serial number", "model", "equipment",
        "location", "site", "installation",
        # Actions
        "lookup", "look up", "find", "search", "get", "fetch",
        "show", "display", "list", "retrieve",
        # Questions about data
        "how many", "what is", "what are", "tell me about",
        "information about", "details about", "data for",
        "status of", "history of",
        # Database/records
        "record", "records", "data", "database", "order", "orders",
        "product", "products", "inventory", "stock",
    ])
    
    # Keywords that indicate simple conversational queries
    conversational_keywords: list[str] = field(default_factory=lambda: [
        "hello", "hi", "hey", "good morning", "good afternoon",
        "good evening", "how are you", "what's up", "thanks",
        "thank you", "bye", "goodbye", "see you", "ok", "okay",
        "yes", "no", "sure", "alright", "got it", "understand",
        "help", "what can you do", "who are you",
    ])
    
    # Question patterns that suggest data queries
    data_question_patterns: list[str] = field(default_factory=lambda: [
        r"^(what|which|where|when|how many|how much)\s+.*(customer|machine|address|order|product)",
        r"(customer|machine|address|order)\s*(id|number|#)?\s*\d+",
        r"(find|search|lookup|get|show)\s+(me\s+)?(the\s+)?(customer|machine|address|data)",
        r"(information|details|data|status)\s+(about|for|on)\s+",
    ])
    
    # Minimum confidence threshold for data lookup classification
    confidence_threshold: float = 0.3


class QueryClassifier(ABC):
    """Abstract base class for query classifiers."""
    
    @abstractmethod
    def classify(self, text: str) -> ClassificationResult:
        """Classify a query and return the result."""
        pass


class KeywordClassifier(QueryClassifier):
    """
    Fast keyword-based query classifier.
    
    This classifier uses keyword matching and regex patterns to quickly
    determine if a query needs agent processing. It's optimized for
    speed to minimize voice latency.
    
    Usage:
        classifier = KeywordClassifier()
        result = classifier.classify("What machines does customer 12345 have?")
        if result.query_type == QueryType.DATA_LOOKUP:
            # Route to agent
    """
    
    def __init__(self, config: Optional[ClassifierConfig] = None):
        self.config = config or ClassifierConfig()
        
        # Pre-compile regex patterns for performance
        self._data_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in self.config.data_question_patterns
        ]
        
        # Normalize keywords to lowercase for matching
        self._data_keywords_lower = [kw.lower() for kw in self.config.data_keywords]
        self._conv_keywords_lower = [kw.lower() for kw in self.config.conversational_keywords]
    
    def classify(self, text: str) -> ClassificationResult:
        """
        Classify a query using keyword matching.
        
        Args:
            text: The user's query text
            
        Returns:
            ClassificationResult with query type, confidence, and details
        """
        if not text or not text.strip():
            return ClassificationResult(
                query_type=QueryType.SIMPLE,
                confidence=1.0,
                reason="Empty query"
            )
        
        text_lower = text.lower().strip()
        matched_keywords = []
        
        # Check for conversational patterns first (quick exit)
        for keyword in self._conv_keywords_lower:
            if keyword in text_lower:
                # But make sure it's not also a data query
                data_score = self._score_data_keywords(text_lower)
                if data_score < 0.2:
                    return ClassificationResult(
                        query_type=QueryType.CONVERSATIONAL,
                        confidence=0.8,
                        matched_keywords=[keyword],
                        reason="Matched conversational keyword"
                    )
        
        # Check regex patterns for data queries
        for pattern in self._data_patterns:
            if pattern.search(text_lower):
                logger.debug(f"Query matched data pattern: {pattern.pattern}")
                return ClassificationResult(
                    query_type=QueryType.DATA_LOOKUP,
                    confidence=0.9,
                    reason=f"Matched data pattern: {pattern.pattern}"
                )
        
        # Score based on keyword matches
        data_score = self._score_data_keywords(text_lower)
        matched_keywords = self._get_matched_keywords(text_lower)
        
        logger.debug(f"Query data score: {data_score}, keywords: {matched_keywords}")
        
        if data_score >= self.config.confidence_threshold:
            return ClassificationResult(
                query_type=QueryType.DATA_LOOKUP,
                confidence=min(data_score, 1.0),
                matched_keywords=matched_keywords,
                reason=f"Keyword score: {data_score:.2f}"
            )
        
        # Default to simple (VoiceLive handles it)
        return ClassificationResult(
            query_type=QueryType.SIMPLE,
            confidence=1.0 - data_score,
            reason="No data lookup indicators found"
        )
    
    def _score_data_keywords(self, text_lower: str) -> float:
        """Calculate a score based on matched data keywords."""
        matches = 0
        for keyword in self._data_keywords_lower:
            if keyword in text_lower:
                matches += 1
                # Weight multi-word keywords higher
                if ' ' in keyword:
                    matches += 0.5
        
        # Normalize score (cap at 1.0)
        # More keywords = higher confidence it's a data query
        if matches == 0:
            return 0.0
        elif matches == 1:
            return 0.4
        elif matches == 2:
            return 0.6
        else:
            return min(0.4 + (matches * 0.2), 1.0)
    
    def _get_matched_keywords(self, text_lower: str) -> list[str]:
        """Get list of matched data keywords."""
        return [kw for kw in self._data_keywords_lower if kw in text_lower]
    
    def add_keyword(self, keyword: str) -> None:
        """Add a new data keyword at runtime."""
        keyword_lower = keyword.lower()
        if keyword_lower not in self._data_keywords_lower:
            self._data_keywords_lower.append(keyword_lower)
            self.config.data_keywords.append(keyword)
    
    def remove_keyword(self, keyword: str) -> None:
        """Remove a data keyword at runtime."""
        keyword_lower = keyword.lower()
        if keyword_lower in self._data_keywords_lower:
            self._data_keywords_lower.remove(keyword_lower)
        if keyword in self.config.data_keywords:
            self.config.data_keywords.remove(keyword)


def create_classifier(config: Optional[ClassifierConfig] = None) -> QueryClassifier:
    """Factory function to create a query classifier."""
    return KeywordClassifier(config)
