package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

type Assignment struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type Command struct {
	Executable  string       `json:"executable"`
	Argv        []string     `json:"argv"`
	Assignments []Assignment `json:"assignments"`
}

type Redirect struct {
	Operator string `json:"operator"`
	Target   string `json:"target"`
	Heredoc  bool   `json:"heredoc"`
}

type Substitution struct {
	Kind string `json:"kind"`
	Text string `json:"text"`
}

type Result struct {
	ParseOK             bool           `json:"parse_ok"`
	Error               string         `json:"error,omitempty"`
	Commands            []Command      `json:"commands"`
	Redirects           []Redirect     `json:"redirects"`
	Substitutions       []Substitution `json:"substitutions"`
	Pipelines           int            `json:"pipelines"`
	Subshells           int            `json:"subshells"`
	BackgroundExecution bool           `json:"background_execution"`
	CompoundCommands    int            `json:"compound_commands"`
	UnresolvedElements  []string       `json:"unresolved_elements"`
}

func render(node syntax.Node) string {
	var b strings.Builder
	p := syntax.NewPrinter(syntax.Minify(true))
	if err := p.Print(&b, node); err != nil {
		return ""
	}
	return b.String()
}

func wordText(word *syntax.Word, unresolved *[]string) string {
	if word == nil {
		return ""
	}
	for _, part := range word.Parts {
		switch part.(type) {
		case *syntax.Lit, *syntax.SglQuoted:
		case *syntax.DblQuoted:
			// Contents are inspected separately by the AST walk.
		default:
			*unresolved = append(*unresolved, fmt.Sprintf("word_expansion:%T", part))
		}
	}
	return render(word)
}

func parse(source string) Result {
	result := Result{ParseOK: false, Commands: []Command{}, Redirects: []Redirect{}, Substitutions: []Substitution{}, UnresolvedElements: []string{}}
	parser := syntax.NewParser(syntax.Variant(syntax.LangBash), syntax.KeepComments(false))
	file, err := parser.Parse(strings.NewReader(source), "command")
	if err != nil {
		result.Error = "bash_parse_error"
		return result
	}
	result.ParseOK = true
	syntax.Walk(file, func(node syntax.Node) bool {
		switch x := node.(type) {
		case *syntax.CallExpr:
			cmd := Command{Argv: []string{}, Assignments: []Assignment{}}
			for _, assign := range x.Assigns {
				name := ""
				if assign.Name != nil {
					name = assign.Name.Value
				}
				cmd.Assignments = append(cmd.Assignments, Assignment{Name: name, Value: wordText(assign.Value, &result.UnresolvedElements)})
			}
			for _, arg := range x.Args {
				cmd.Argv = append(cmd.Argv, wordText(arg, &result.UnresolvedElements))
			}
			if len(cmd.Argv) > 0 {
				cmd.Executable = cmd.Argv[0]
			}
			result.Commands = append(result.Commands, cmd)
		case *syntax.Redirect:
			result.Redirects = append(result.Redirects, Redirect{Operator: x.Op.String(), Target: wordText(x.Word, &result.UnresolvedElements), Heredoc: x.Hdoc != nil})
		case *syntax.CmdSubst:
			kind := "dollar_paren"
			if x.Backquotes {
				kind = "backticks"
			}
			result.Substitutions = append(result.Substitutions, Substitution{Kind: kind, Text: render(x)})
		case *syntax.ProcSubst:
			result.Substitutions = append(result.Substitutions, Substitution{Kind: "process", Text: render(x)})
			result.UnresolvedElements = append(result.UnresolvedElements, "process_substitution")
		case *syntax.BinaryCmd:
			if x.Op == syntax.Pipe || x.Op == syntax.PipeAll {
				result.Pipelines++
			} else {
				result.CompoundCommands++
			}
		case *syntax.Subshell:
			result.Subshells++
		case *syntax.Stmt:
			if x.Background {
				result.BackgroundExecution = true
			}
		case *syntax.ArithmExp:
			result.UnresolvedElements = append(result.UnresolvedElements, "arithmetic_expansion")
		}
		return true
	})
	return result
}

func main() {
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, (1<<20)+1))
	if err != nil {
		os.Exit(2)
	}
	if len(raw) > 1<<20 {
		_ = json.NewEncoder(os.Stdout).Encode(Result{ParseOK: false, Error: "command_too_large"})
		os.Exit(1)
	}
	result := parse(string(raw))
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(result); err != nil {
		os.Exit(2)
	}
	if !result.ParseOK {
		os.Exit(1)
	}
}
