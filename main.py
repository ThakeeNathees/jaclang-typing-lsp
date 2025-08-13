def main():
    print("Hello from pyrnd!")


if __name__ == "__main__":
    main()

def foo(x: int | str):
    if isinstance(x, str):
        if input('a') == 'a':
            return 'i'
