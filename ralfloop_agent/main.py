from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub


def main() -> None:
    adapter = OpenShellAdapterStub()
    sandbox = adapter.create_sandbox()

    try:
        exec_result = adapter.exec(
            sandbox,
            "mkdir -p out && echo 'hello from sandbox_exec' > out/hello_exec.txt && cat out/hello_exec.txt",
        )
        print("EXEC:")
        print(exec_result.stdout.strip())
        if exec_result.stderr:
            print(exec_result.stderr.strip())

        list_result = adapter.list_dir(sandbox, "out")
        print("\nLIST:")
        print(list_result.stdout.strip())

        read_result = adapter.read_file(sandbox, "out/hello_exec.txt")
        print("\nREAD:")
        print(read_result.stdout.strip())
    finally:
        adapter.destroy_sandbox(sandbox["id"])


if __name__ == "__main__":
    main()
