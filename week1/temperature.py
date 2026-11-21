import math


def read_temperature(prompt):
    while True:
        try:
            temperature = float(input(prompt))
            if math.isfinite(temperature):
                return temperature
        except ValueError:
            pass
        print("请输入数字")


def main():
    while True:
        choice = input("输入 1 是摄氏转华氏，2 是华氏转摄氏：").strip()
        if choice in ("1", "2"):
            break
        if choice.isdecimal():
            print("请输入 1 或 2")
        else:
            print("请输入数字")

    if choice == "1":
        celsius = read_temperature("请输入摄氏度：")
        fahrenheit = celsius * (9 / 5) + 32
        print(f"{fahrenheit:.2f} °F")
    else:
        fahrenheit = read_temperature("请输入华氏度：")
        celsius = (fahrenheit - 32) * (5 / 9)
        print(f"{celsius:.2f} °C")


if __name__ == "__main__":
    main()